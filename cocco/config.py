"""What cocco is configured with, and where each half of it comes from.

Two sources, and the line between them is one sentence: **the file is
settings, the environment is secrets**. The database, the storage root, the
domain allowlist, the bootstrap administrators and the development shortcuts
are written in `cocco.toml`; the session signing key and the OAuth clients are
read from `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID`,
`COCCO_GOOGLE_CLIENT_SECRET`, `COCCO_GITHUB_CLIENT_ID` and
`COCCO_GITHUB_CLIENT_SECRET` and have no file spelling at all. One source
each, so there is no precedence to reason about and nothing secret in a file
that gets copied, diffed and pasted into a bug report.

`COCCO_CONFIG` is the exception that proves it: it names the file, so it is
the one setting that cannot itself be written in one.

Its own module rather than a corner of `auth.py`, because both processes read
it - the judge needs the database and the storage root, and neither is any of
sign-in's business.
"""

import ipaddress
import logging
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cocco.auth import is_allowed_email, normalize_administrators, normalize_domains

logger = logging.getLogger(__name__)

# The institution, and the default allowlist. A domain admits its subdomains,
# so this one entry already covers @studenti.unipd.it and @phd.unipd.it.
DEFAULT_ALLOWED_DOMAINS = ("unipd.it",)

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

DEFAULT_CONFIG_PATH = Path("cocco.toml")

# The empty URL is not a placeholder. libpq fills the user, database, host and
# password in from PGUSER/PGDATABASE/PGHOST/PGPASSWORD and ~/.pgpass, which is
# how the Nix devShell's socket-only Postgres is reached without writing an
# absolute path down: PGHOST there is `$PWD/.pg/sock`, and a file in git cannot
# know what $PWD is. This is not the environment configuring cocco - it is
# psycopg reading its own standard variables.
#
# psycopg is named rather than left to SQLAlchemy's default because it is the
# one driver with both a sync and an async face: `main.py` builds an async
# engine from this string and `judge.py` a sync one, and they must be able to
# be the same string.
DEFAULT_DATABASE_URL = "postgresql+psycopg://"

DEFAULT_STORAGE_ROOT = Path("/tmp/cocco-storage")

# The flake `nix build` builds a language runtime out of. "." is the working
# directory, which is what this was before it could be written down at all -
# so the default changes nothing for a judge started from a checkout.
DEFAULT_FLAKE_REF = "."

# How many isolate boxes one judge may have open at once - the size of
# `cocco.judge._IDS_POOL`. Here rather than in `judge.py` because
# `[judge] concurrency` is checked against it at load, and a judge that waits on
# its own pool for longer than it waits on the queue is a setting with no use.
BOX_POOL_SIZE = 32


# Who may tell cocco, through X-Forwarded-For and X-Forwarded-Proto, who the
# client really is. Loopback in both spellings, because that is a reverse proxy
# on the same host - nginx reaching the app over 127.0.0.1, or over ::1 when
# `localhost` resolves that way first.
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")

LOG_FORMATS = ("text", "json")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """A setting the file spells in a way cocco cannot act on.

    Rare on purpose: almost everything in this module warns and carries on,
    because a stale line must not stop a deployment from booting. The
    exceptions are the settings whose wrong spelling has no safe reading - a
    log format nobody can parse, a proxy address that would trust the wrong
    peer - and those are refused at load, naming what is accepted.
    """


def default_gcroots() -> Path:
    """Where the judge keeps the symlinks that hold its runtimes against the
    nix garbage collector.

    A function rather than a `DEFAULT_*` constant like every setting around it,
    because it reads the environment: a constant would bake `HOME` in at import
    time, which is untestable and wrong for a process systemd starts with a
    different one.

    `$XDG_STATE_HOME` and deliberately not `$XDG_CACHE_HOME`: a cache is by
    definition safe to delete, and deleting these is the exact bug they exist
    to prevent. `/tmp` would be worse still - systemd-tmpfiles would remove the
    links and silently un-root the store paths, which is where this started.
    """
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "cocco" / "gcroots"


def default_concurrency() -> int:
    """How many jobs a judge has in flight when `[judge] concurrency` is unset:
    half the host's hardware threads, within the box pool.

    A function for `default_gcroots`'s reason - it reads the machine, and a
    constant would read the one the module was imported on. Sized against the
    CPU and not against the pool because the CPU is what runs out first: a box
    is mostly a python starting up, and on a 16-thread host the judge stopped
    getting faster at about 8 jobs in flight, with every box after that only
    making the others slower. Half rather than all of them because hardware
    threads are not cores, and the host also runs the compiler outside the box
    and PostgreSQL.
    """
    # `process_cpu_count`, not `cpu_count`: the threads this process may run
    # on, so a judge pinned to a few cores by systemd or a container sizes
    # itself against those rather than against the whole machine.
    return max(1, min(BOX_POOL_SIZE, (os.process_cpu_count() or 2) // 2))


@dataclass(frozen=True)
class SessionConfig:
    secret_key: str
    https_only: bool = False
    max_age: int = 14 * 24 * 3600
    cookie_name: str = "cocco_session"


@dataclass(frozen=True)
class GoogleConfig:
    """The registered OAuth client. Both halves are required, so a
    `GoogleConfig` that exists is one a flow can actually be started with."""

    client_id: str
    client_secret: str
    # Google's `hd` hint, passed to the account chooser. Deliberately its own
    # setting rather than something derived from `allowed_domains`: `hd` takes a
    # single domain, and a Workspace's secondary domains are not always
    # reachable through the parent's hint. Never the access check.
    hd: str | None = None
    server_metadata_url: str = GOOGLE_METADATA_URL


@dataclass(frozen=True)
class GithubConfig:
    """The registered OAuth app, and deliberately shorter than `GoogleConfig`.

    GitHub is plain OAuth2 and not OIDC, so there is no metadata document to
    point at and no ID token to verify; the callback asks the API which of the
    account's addresses are verified. Nor is there an `hd`: GitHub has no
    notion of a Workspace domain to hint at, and `allowed_domains` was always
    the thing that actually decides.
    """

    client_id: str
    client_secret: str


@dataclass(frozen=True)
class AuthConfig:
    # Suffix-matched on a dot boundary: "unipd.it" admits unipd.it and every
    # subdomain of it. See `is_allowed_email`.
    allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS
    # None means Google sign-in is unconfigured, which is not an error: the app
    # boots and the login page says so. `[dev] dummy_auth` needs no credentials.
    google: GoogleConfig | None = None
    # The second provider, on exactly the same terms: None means unconfigured,
    # which is not an error. Each is enabled by its own pair of environment
    # variables and neither knows about the other.
    github: GithubConfig | None = None
    # The site's first administrators, normalized. A *bootstrap*, not the
    # truth: they are inserted into `administrator` on every boot and the table
    # is what is asked afterwards, so this list can only ever add. An empty one
    # is the ordinary case for a database that already has its administrators.
    administrators: tuple[str, ...] = ()
    # The second way in, and off unless a deployment says otherwise. False is
    # not timidity about a feature: Google proves that the person owns the
    # mailbox, and a password proves only that somebody typed an address this
    # app has no way to send anything to. See `cocco.example.toml`, which
    # spells out what a deployment is accepting when it turns this on.
    password_login: bool = False


@dataclass(frozen=True)
class DatabaseConfig:
    # One URL rather than a host/user/database triple, because SQLAlchemy's
    # currency is a URL: assembling one is precisely the hand-rolled f-string
    # this setting exists to delete, and doing it a layer down in two processes
    # that must agree would be a strange fix for the bug that they disagreed.
    # A password may be left out of it and supplied by ~/.pgpass or PGPASSWORD.
    url: str = DEFAULT_DATABASE_URL


@dataclass(frozen=True)
class StorageConfig:
    # The web app and the judge must resolve a test case's path to the same
    # bytes; they agree by reading this one key out of the same file.
    root: Path = DEFAULT_STORAGE_ROOT


@dataclass(frozen=True)
class JudgeConfig:
    """The settings only the judge reads, and the reason the first is a
    setting at all: a language entry in `languages.toml` names a runtime that
    some flake has to expose, and `build_runtime` shells out to `nix build` to
    get it - once for each, at startup, from `prewarm_runtimes`.

    That used to be spelled `.#python3` - the flake in the *working directory*
    - which is a coupling the judge could not state and a deployment could not
    satisfy: an installed `cocco-judge` on PATH has no checkout to be started
    from. So the ref is written down instead, and "." is what it always was.

    Deliberately not the same kind of setting as `[storage] root`: that one
    exists so the two processes agree, and this one the web app never reads.
    """

    # A path (relative or absolute) or any flake reference `nix` accepts, e.g.
    # "/opt/cocco" or "github:you/cocco".
    flake: str = DEFAULT_FLAKE_REF

    # Where `nix build --out-link` puts one symlink per runtime, which is what
    # registers the *indirect GC root* that stops `nix-store --gc` collecting
    # an interpreter the judge is using. It built with `--no-link` before, so
    # the store path was held by nothing nix could see and a garbage collection
    # on the judge host could pull the interpreter out from under a submission.
    #
    # Per judge host and per deployment, and that is the one trap: two judges
    # sharing this directory with different `flake` refs would overwrite each
    # other's links, and a collection in between could take a live runtime.
    #
    # Deliberately not derived from `[storage] root`. That is the one directory
    # the web app and the judge share and it may well be on NFS; a GC root is
    # local to the machine whose store it protects, and means nothing on another.
    gcroots: Path = field(default_factory=default_gcroots)

    # How many jobs the judge works on at once, each a worker task claiming the
    # next one off the queue. Between 1 and `BOX_POOL_SIZE`: past the pool, the
    # extra workers would only wait for a box.
    concurrency: int = field(default_factory=default_concurrency)


@dataclass(frozen=True)
class DevConfig:
    """The development shortcuts, and every one of them is a hazard on a real
    deployment - which is why they are one table. A production `cocco.toml` can
    then be checked by the *absence* of `[dev]`, one thing to look for, rather
    than by three keys scattered among the ordinary settings where a stray
    `reset_db = true` reads like a normal line.

    Deliberately not `DevConfig | None`, unlike `GoogleConfig`: there, None is
    a state the app reports to the user ("Google sign-in is unconfigured");
    here, "no [dev] table" and "all of them off" are the same thing.
    """

    # No Google, no credentials: `/login` grows a box you type an address into
    # and are signed in as, whoever it names. On anything public this is
    # equivalent to publishing every account's password.
    dummy_auth: bool = False
    # Drops every table and reseeds. `fastapi dev` reloads on every file
    # change, and each reload would wipe the data.
    reset_db: bool = False


@dataclass(frozen=True)
class LogConfig:
    """How every process writes its log, which is stdout and nothing else.

    Text is the default so a terminal in development stays readable; a deployment asks for
    one JSON object per line, which is what a collector can query. Where the
    lines go after stdout - journald, a container runtime, a file rotated by
    logrotate - is the collector's business, and so is how long they are kept.
    """

    format: Literal["text", "json"] = "text"
    level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    session: SessionConfig
    auth: AuthConfig
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    dev: DevConfig = field(default_factory=DevConfig)
    log: LogConfig = field(default_factory=LogConfig)
    # Absolute base for the OAuth redirect URI. None derives it from the
    # incoming request, which is only right behind a proxy that sets
    # X-Forwarded-* and `proxy_headers` below.
    base_url: str | None = None
    # Whether X-Forwarded-For / -Proto (and X-Request-ID) are believed at all.
    # False, as the `--proxy-headers` flag it replaces was: a checkout with no
    # proxy in front must not let a client name its own address.
    proxy_headers: bool = False
    # And from whom: addresses and CIDR networks, already validated. Settings
    # rather than uvicorn's FORWARDED_ALLOW_IPS, which is the environment.
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES


def config_path() -> Path:
    """Which file to read. The one thing that cannot be written in one.

    Spelled here rather than in each process, so `main.py` and `judge.py`
    cannot end up reading different files by default.
    """
    return Path(os.environ.get("COCCO_CONFIG") or DEFAULT_CONFIG_PATH)


def load_config(path: Path) -> AppConfig:
    """Read `cocco.toml`, in the shape `load_languages` reads `languages.toml`.

    A missing file is not an error - a checkout that has not been configured at
    all must still boot, and with `DEFAULT_DATABASE_URL` it boots against
    whatever database the surrounding shell already points `psql` at. Neither
    is an `[auth]` table with no credentials in it: the app comes up and the
    login page says Google sign-in is unconfigured.
    """
    data: dict = {}
    if path.is_file():
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    else:
        logger.info("no configuration file at %s; using defaults", path)

    _warn_about_removed_settings(path, data)

    site = data.get("site", {})
    return AppConfig(
        session=_load_session_config(data.get("session", {})),
        auth=_load_auth_config(data.get("auth", {})),
        database=_load_database_config(data.get("database", {})),
        storage=_load_storage_config(data.get("storage", {})),
        judge=_load_judge_config(data.get("judge", {})),
        dev=_load_dev_config(data.get("dev", {})),
        log=_load_log_config(data.get("log", {})),
        base_url=site.get("base_url"),
        proxy_headers=bool(site.get("proxy_headers", False)),
        trusted_proxies=_load_trusted_proxies(site),
    )


def load_log_config(path: Path) -> LogConfig:
    """The `[log]` table alone, without anything `load_config` has to say.

    A process has to configure logging *before* it reads the rest of the file,
    or the warnings `load_config` emits - a missing file, a setting in its old
    place, no session secret - would come out in whatever format Python's
    last-resort handler uses rather than the one the deployment asked for.
    """
    data: dict = {}
    if path.is_file():
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    return _load_log_config(data.get("log", {}))


def _load_session_config(section: dict) -> SessionConfig:
    # The environment, and nothing else: a signing key has one source, so
    # there is no precedence to get wrong and none of it is ever committed.
    secret_key = os.environ.get("COCCO_SECRET_KEY")
    if not secret_key:
        # Deliberately not a constant: a fixed fallback secret is a signing key
        # that ships in the source tree. A fresh random one per process fails
        # loudly instead - everyone is logged out on restart, and workers do
        # not agree - which is noticeable long before it is dangerous.
        secret_key = secrets.token_urlsafe(32)
        logger.warning(
            "no session secret configured (COCCO_SECRET_KEY); generated an "
            "ephemeral one, so every restart logs everybody out",
            extra={"setting": "COCCO_SECRET_KEY"},
        )
    return SessionConfig(
        secret_key=secret_key,
        https_only=bool(section.get("https_only", False)),
        max_age=int(section.get("max_age", SessionConfig.max_age)),
        cookie_name=section.get("cookie_name", SessionConfig.cookie_name),
    )


def _oauth_credentials(provider: str) -> tuple[str, str] | None:
    """The two halves of one provider's OAuth client, or None.

    The environment and only the environment: `COCCO_GOOGLE_CLIENT_ID` and
    `COCCO_GITHUB_CLIENT_SECRET` are the same kind of thing spelled twice, so
    the reading of them is written once. Both halves are required, which is
    what makes a provider config that exists one a flow can be started with -
    and half a credential is a typo, not a decision, so it says so and then
    carries on unconfigured rather than refusing to boot.
    """
    prefix = f"COCCO_{provider.upper()}_"
    client_id = os.environ.get(prefix + "CLIENT_ID")
    client_secret = os.environ.get(prefix + "CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    if client_id or client_secret:
        logger.warning(
            "%s sign-in needs both %sCLIENT_ID and %sCLIENT_SECRET in the "
            "environment; it is disabled",
            provider,
            prefix,
            prefix,
            extra={"setting": prefix + "CLIENT_ID/SECRET"},
        )
    return None


def _load_auth_config(section: dict) -> AuthConfig:
    """The `[auth]` table - which is the settings half of signing in. The
    credentials themselves come from the environment, and an absent setting is
    a default rather than an error."""
    raw_domains = section.get("allowed_domains")
    domains = (
        normalize_domains(raw_domains)
        if raw_domains is not None
        else DEFAULT_ALLOWED_DOMAINS
    )

    administrators = normalize_administrators(section.get("administrators") or ())
    for email in administrators:
        if not is_allowed_email(email, domains):
            # Not fatal, and not filtered out either - the table is the truth
            # and someone may be fixing the allowlist next. But an
            # administrator who cannot sign in is a typo worth naming.
            logger.warning(
                "[auth] administrator %s is outside `allowed_domains`, "
                "so they could never sign in",
                email,
                extra={"setting": "[auth] administrators"},
            )

    google = None
    credentials = _oauth_credentials("google")
    if credentials is not None:
        google = GoogleConfig(
            client_id=credentials[0],
            client_secret=credentials[1],
            hd=section.get("google_hd"),
            server_metadata_url=section.get(
                "google_metadata_url", GOOGLE_METADATA_URL
            ),
        )

    github = None
    credentials = _oauth_credentials("github")
    if credentials is not None:
        github = GithubConfig(client_id=credentials[0], client_secret=credentials[1])

    return AuthConfig(
        allowed_domains=domains,
        google=google,
        github=github,
        administrators=administrators,
        # A plain bool and no warning when it is on: unlike half a Google
        # credential, this one cannot be a typo - it is a switch, and the file
        # is the only place it can be spelled.
        password_login=bool(section.get("password_login", False)),
    )


def _load_database_config(section: dict) -> DatabaseConfig:
    # `or`, not a default on `get`: an explicit `url = ""` means the same as no
    # url at all, rather than an engine that fails somewhere further down.
    return DatabaseConfig(url=section.get("url") or DEFAULT_DATABASE_URL)


def _load_storage_config(section: dict) -> StorageConfig:
    root = section.get("root")
    return StorageConfig(root=Path(root) if root else DEFAULT_STORAGE_ROOT)


def _load_judge_config(section: dict) -> JudgeConfig:
    # `or`, like the database URL: an explicit `flake = ""` is a typo, not a
    # request for `nix build #python3` - and an empty `gcroots` is a typo, not
    # a request to scatter symlinks over the filesystem root.
    gcroots = section.get("gcroots")
    return JudgeConfig(
        flake=section.get("flake") or DEFAULT_FLAKE_REF,
        gcroots=Path(gcroots) if gcroots else default_gcroots(),
        concurrency=_load_concurrency(section),
    )


def _load_concurrency(section: dict) -> int:
    # Refused rather than defaulted, `[log] format`'s rule: a deployment that
    # wrote `concurrency = 0` has a judge that never grades anything, and one
    # that wrote 100 believes it is getting a throughput the box pool will not
    # give it. Neither has a reading that is safe to guess. `bool` is excluded
    # by name because TOML's `true` is an `int` to Python.
    if "concurrency" not in section:
        return default_concurrency()
    value = section["concurrency"]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= BOX_POOL_SIZE
    ):
        raise ConfigError(
            f"[judge] concurrency = {value!r} must be a whole number from 1 to "
            f"{BOX_POOL_SIZE}, the judge's box pool"
        )
    return value


def _load_log_config(section: dict) -> LogConfig:
    # Refused rather than defaulted: a deployment that wrote `format = "jsonl"`
    # is expecting a collector to parse what comes out, and quietly handing it
    # text would be a log nobody notices is unreadable until it is needed.
    log_format = section.get("format", LogConfig.format)
    if log_format not in LOG_FORMATS:
        raise ConfigError(
            f"[log] format = {log_format!r} is not one of "
            + ", ".join(repr(f) for f in LOG_FORMATS)
        )
    level = str(section.get("level", LogConfig.level)).upper()
    if level not in LOG_LEVELS:
        raise ConfigError(
            f"[log] level = {section.get('level')!r} is not one of "
            + ", ".join(LOG_LEVELS)
        )
    return LogConfig(format=log_format, level=level)


def _load_trusted_proxies(site: dict) -> tuple[str, ...]:
    """`[site] trusted_proxies`, each entry checked for being an address or a
    network.

    Checked here because uvicorn does not: its `_TrustedHosts` keeps anything
    that fails to parse as a literal string, so `127.0.0.1/33` or a hostname
    would be accepted, match nothing, and leave every request logged with the
    proxy's address - the exact failure this setting exists to prevent, with
    nothing to say why.
    """
    raw = site.get("trusted_proxies")
    if raw is None:
        return DEFAULT_TRUSTED_PROXIES
    if isinstance(raw, str) or not isinstance(raw, list):
        raise ConfigError(
            "[site] trusted_proxies must be a list of addresses or networks, "
            'e.g. ["127.0.0.1", "10.0.0.0/8"]'
        )
    proxies = []
    for entry in raw:
        try:
            if "/" in str(entry):
                ipaddress.ip_network(entry)
            else:
                ipaddress.ip_address(entry)
        except ValueError:
            raise ConfigError(
                f"[site] trusted_proxies: {entry!r} is neither an IP address "
                "nor a CIDR network"
            ) from None
        proxies.append(str(entry))
    return tuple(proxies)


def _load_dev_config(section: dict) -> DevConfig:
    return DevConfig(
        dummy_auth=bool(section.get("dummy_auth", False)),
        reset_db=bool(section.get("reset_db", False)),
    )


########################################################################
# migration

# Configuration used to be spread over the environment, and the secrets used to
# have a file spelling as well. Both halves moved, and a setting in its old
# place is now silently ignored - which is at worst dangerous: COCCO_DUMMY_AUTH=0
# is how somebody turns the shortcut *off* to exercise the real sign-in flow,
# and a `secret_key` still sitting in a deployment's `cocco.toml` means that
# deployment loses its signing key and its Google client on the next restart.
#
# So each one is named out loud, and only warned about: a stale variable in
# somebody's shell must not stop the app from booting, and there is a human in
# front of the log who can fix it. Delete this block once the deployments have
# moved.
_REMOVED_ENV = {
    "COCCO_DUMMY_AUTH": "[dev] dummy_auth",
    "COCCO_RESET_DB": "[dev] reset_db",
    "COCCO_STORAGE": "[storage] root",
}

_REMOVED_KEYS = {
    ("session", "secret_key"): "COCCO_SECRET_KEY",
    ("auth", "google_client_id"): "COCCO_GOOGLE_CLIENT_ID",
    ("auth", "google_client_secret"): "COCCO_GOOGLE_CLIENT_SECRET",
}

# The third kind, and it is neither of the two above: both of those say "that
# setting moved *there*", and this one moved nowhere at all. `dummy_user` used
# to name the single address every request was signed in as; with `[dev]
# dummy_auth` on, `/login` now asks. One sentence, checked against both of its
# spellings below - the environment's and the file's - because either one left
# behind means somebody is expecting to be signed in as somebody in particular.
_GONE_TO_THE_LOGIN_BOX = (
    "there is no such setting any more: with `[dev] dummy_auth` on, /login asks "
    "which address to sign in as"
)


def _warn_about_removed_settings(path: Path, data: dict) -> None:
    for variable, replacement in _REMOVED_ENV.items():
        if variable in os.environ:
            logger.warning(
                "%s is no longer read; that setting is `%s` in %s now",
                variable,
                replacement,
                path,
                extra={"setting": variable},
            )
    for (table, key), replacement in _REMOVED_KEYS.items():
        if key in data.get(table, {}):
            logger.warning(
                "[%s] %s in %s is no longer read; it is the %s environment "
                "variable now, and only that",
                table,
                key,
                path,
                replacement,
                extra={"setting": f"[{table}] {key}"},
            )
    if "COCCO_DUMMY_USER" in os.environ:
        logger.warning(
            "COCCO_DUMMY_USER is no longer read; %s",
            _GONE_TO_THE_LOGIN_BOX,
            extra={"setting": "COCCO_DUMMY_USER"},
        )
    if "dummy_user" in data.get("dev", {}):
        logger.warning(
            "[dev] dummy_user in %s is no longer read; %s",
            path,
            _GONE_TO_THE_LOGIN_BOX,
            extra={"setting": "[dev] dummy_user"},
        )


# PGUSER/PGDATABASE/PGHOST are deliberately absent from `_REMOVED_ENV`: they
# are libpq's own, they are still read - by psycopg, on purpose - and they are
# the whole mechanism behind `DEFAULT_DATABASE_URL`.
