"""What cocco is configured with, and where each half of it comes from.

Two sources, and the line between them is one sentence: **the file is
settings, the environment is secrets**. The database, the storage root, the
domain allowlist, the bootstrap administrators and the development shortcuts
are written in `cocco.toml`; the session signing key and the Google client are
read from `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID` and
`COCCO_GOOGLE_CLIENT_SECRET` and have no file spelling at all. One source
each, so there is no precedence to reason about and nothing secret in a file
that gets copied, diffed and pasted into a bug report.

`COCCO_CONFIG` is the exception that proves it: it names the file, so it is
the one setting that cannot itself be written in one.

Its own module rather than a corner of `auth.py`, because both processes read
it - the judge needs the database and the storage root, and neither is any of
sign-in's business.
"""

import logging
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
class AuthConfig:
    # Suffix-matched on a dot boundary: "unipd.it" admits unipd.it and every
    # subdomain of it. See `is_allowed_email`.
    allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS
    # None means Google sign-in is unconfigured, which is not an error: the app
    # boots and the login page says so. `[dev] dummy_auth` needs no credentials.
    google: GoogleConfig | None = None
    # The site's first administrators, normalized. A *bootstrap*, not the
    # truth: they are inserted into `administrator` on every boot and the table
    # is what is asked afterwards, so this list can only ever add. An empty one
    # is the ordinary case for a database that already has its administrators.
    administrators: tuple[str, ...] = ()


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
    """The one thing only the judge reads, and the only reason it is a setting
    at all: a language entry in `languages.toml` names a runtime that some
    flake has to expose, and `resolve_runtime` shells out to `nix build` to get
    it.

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

    # No Google, no click, no credentials: `get_user` signs every request in.
    # On anything public this is equivalent to publishing the admin password.
    dummy_auth: bool = False
    # None rather than the seeded administrator: that cast lives in `main.py`,
    # beside the seed that writes it, and supplies the fallback.
    dummy_user: str | None = None
    # Drops every table and reseeds. `fastapi dev` reloads on every file
    # change, and each reload would wipe the data.
    reset_db: bool = False


@dataclass(frozen=True)
class AppConfig:
    session: SessionConfig
    auth: AuthConfig
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    dev: DevConfig = field(default_factory=DevConfig)
    # Absolute base for the OAuth redirect URI. None derives it from the
    # incoming request, which is only right behind a proxy that sets
    # X-Forwarded-* and a uvicorn running with --proxy-headers.
    base_url: str | None = None


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

    return AppConfig(
        session=_load_session_config(data.get("session", {})),
        auth=_load_auth_config(data.get("auth", {})),
        database=_load_database_config(data.get("database", {})),
        storage=_load_storage_config(data.get("storage", {})),
        judge=_load_judge_config(data.get("judge", {})),
        dev=_load_dev_config(data.get("dev", {})),
        base_url=data.get("site", {}).get("base_url"),
    )


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
            "ephemeral one, so every restart logs everybody out"
        )
    return SessionConfig(
        secret_key=secret_key,
        https_only=bool(section.get("https_only", False)),
        max_age=int(section.get("max_age", SessionConfig.max_age)),
        cookie_name=section.get("cookie_name", SessionConfig.cookie_name),
    )


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

    client_id = os.environ.get("COCCO_GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("COCCO_GOOGLE_CLIENT_SECRET")

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
            )

    google = None
    if client_id and client_secret:
        google = GoogleConfig(
            client_id=client_id,
            client_secret=client_secret,
            hd=section.get("google_hd"),
            server_metadata_url=section.get(
                "google_metadata_url", GOOGLE_METADATA_URL
            ),
        )
    elif client_id or client_secret:
        # Half a credential is a typo, not a decision: say so, then carry on
        # unconfigured rather than refusing to boot.
        logger.warning(
            "Google sign-in needs both COCCO_GOOGLE_CLIENT_ID and "
            "COCCO_GOOGLE_CLIENT_SECRET in the environment; it is disabled"
        )
    return AuthConfig(
        allowed_domains=domains, google=google, administrators=administrators
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
    # request for `nix build #python3`.
    return JudgeConfig(flake=section.get("flake") or DEFAULT_FLAKE_REF)


def _load_dev_config(section: dict) -> DevConfig:
    return DevConfig(
        dummy_auth=bool(section.get("dummy_auth", False)),
        dummy_user=section.get("dummy_user") or None,
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
    "COCCO_DUMMY_USER": "[dev] dummy_user",
    "COCCO_RESET_DB": "[dev] reset_db",
    "COCCO_STORAGE": "[storage] root",
}

_REMOVED_KEYS = {
    ("session", "secret_key"): "COCCO_SECRET_KEY",
    ("auth", "google_client_id"): "COCCO_GOOGLE_CLIENT_ID",
    ("auth", "google_client_secret"): "COCCO_GOOGLE_CLIENT_SECRET",
}


def _warn_about_removed_settings(path: Path, data: dict) -> None:
    for variable, replacement in _REMOVED_ENV.items():
        if variable in os.environ:
            logger.warning(
                "%s is no longer read; that setting is `%s` in %s now",
                variable,
                replacement,
                path,
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
            )


# PGUSER/PGDATABASE/PGHOST are deliberately absent from `_REMOVED_ENV`: they
# are libpq's own, they are still read - by psycopg, on purpose - and they are
# the whole mechanism behind `DEFAULT_DATABASE_URL`.
