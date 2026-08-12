"""Google sign-in: configuration, the domain allowlist and the account behind
a confirmed address.

Nothing here talks to Google. The OAuth client and the routes live in
`main.py`, where the request is; what is left is pure and unit-testable, except
`get_or_create_user`, which is the one place the flow touches the schema.
"""

import logging
import os
import secrets
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.avatar import generate_identicon
from cocco.repository import insert_user, query_user_by_email

logger = logging.getLogger(__name__)

# The institution, and the default allowlist. A domain admits its subdomains,
# so this one entry already covers @studenti.unipd.it and @phd.unipd.it.
DEFAULT_ALLOWED_DOMAINS = ("unipd.it",)

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

# Where Google sends the browser back. A constant because it must match, byte
# for byte, an "Authorized redirect URI" registered in the Google Cloud
# console - it is not something a route can invent per request.
GOOGLE_CALLBACK_PATH = "/auth/google/callback"

# RFC 5321's ceiling. Checked before the address is looked at any further, so a
# preposterous claim costs nothing.
MAX_EMAIL_LENGTH = 254


########################################################################
# configuration


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
    # boots and the login page says so. COCCO_DUMMY_AUTH=1 needs no credentials.
    google: GoogleConfig | None = None
    # The site's first administrators, normalized. A *bootstrap*, not the
    # truth: they are inserted into `administrator` on every boot and the table
    # is what is asked afterwards, so this list can only ever add. An empty one
    # is the ordinary case for a database that already has its administrators.
    administrators: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    session: SessionConfig
    auth: AuthConfig
    # Absolute base for the OAuth redirect URI. None derives it from the
    # incoming request, which is only right behind a proxy that sets
    # X-Forwarded-* and a uvicorn running with --proxy-headers.
    base_url: str | None = None


def load_config(path: Path) -> AppConfig:
    """Read `cocco.toml`, in the shape `load_languages` reads `languages.toml`.

    A missing file is not an error - a development checkout has none and must
    still boot. Neither is an `[auth]` table with no credentials in it: the app
    comes up and the login page says Google sign-in is unconfigured.
    """
    data: dict = {}
    if path.is_file():
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    else:
        logger.info("no configuration file at %s; using defaults", path)

    return AppConfig(
        session=_load_session_config(data.get("session", {})),
        auth=_load_auth_config(data.get("auth", {})),
        base_url=data.get("site", {}).get("base_url"),
    )


def _load_session_config(section: dict) -> SessionConfig:
    secret_key = os.environ.get("COCCO_SECRET_KEY") or section.get("secret_key")
    if not secret_key:
        # Deliberately not a constant: a fixed fallback secret is a signing key
        # that ships in the source tree. A fresh random one per process fails
        # loudly instead - everyone is logged out on restart, and workers do
        # not agree - which is noticeable long before it is dangerous.
        secret_key = secrets.token_urlsafe(32)
        logger.warning(
            "no session secret configured (COCCO_SECRET_KEY or [session].secret_key); "
            "generated an ephemeral one, so every restart logs everybody out"
        )
    return SessionConfig(
        secret_key=secret_key,
        https_only=bool(section.get("https_only", False)),
        max_age=int(section.get("max_age", SessionConfig.max_age)),
        cookie_name=section.get("cookie_name", SessionConfig.cookie_name),
    )


def _load_auth_config(section: dict) -> AuthConfig:
    """The `[auth]` table, with the same precedence `_load_session_config`
    uses: the environment wins over the file, and an absent setting is a
    default rather than an error."""
    raw_domains = section.get("allowed_domains")
    domains = (
        normalize_domains(raw_domains)
        if raw_domains is not None
        else DEFAULT_ALLOWED_DOMAINS
    )

    client_id = os.environ.get("COCCO_GOOGLE_CLIENT_ID") or section.get(
        "google_client_id"
    )
    client_secret = os.environ.get("COCCO_GOOGLE_CLIENT_SECRET") or section.get(
        "google_client_secret"
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
            "[auth] needs both `google_client_id` and `google_client_secret` "
            "(or COCCO_GOOGLE_CLIENT_ID / COCCO_GOOGLE_CLIENT_SECRET); "
            "Google sign-in is disabled"
        )
    return AuthConfig(
        allowed_domains=domains, google=google, administrators=administrators
    )


########################################################################
# who may sign in


def normalize_domains(raw: Iterable[str]) -> tuple[str, ...]:
    """The configured allowlist, canonicalized: lowercase, trimmed, no leading
    or trailing dot. Done once at load so the predicate has nothing to clean."""
    cleaned = (d.strip().lower().strip(".") for d in raw if d)
    return tuple(d for d in cleaned if d)


def normalize_administrators(raw: Iterable[str]) -> tuple[str, ...]:
    """The configured administrators, canonicalized to what `administrator`
    stores: `normalize_email` on each, in the order given, without duplicates.

    An entry that is not an address at all is dropped with a warning rather
    than raising: one bad line in the list must not stop the app from booting,
    and the ones around it are still perfectly good administrators.
    """
    seen: dict[str, None] = {}
    for entry in raw:
        try:
            seen.setdefault(normalize_email(entry), None)
        except ValueError as exc:
            logger.warning("[auth] ignoring administrator %r: %s", entry, exc)
    return tuple(seen)


def email_domain(email: str) -> str:
    """The part after the last `@`, lowercased. Empty when there is no `@`."""
    _, at, domain = email.rpartition("@")
    if not at:
        return ""
    return domain.strip().lower().rstrip(".")


def is_allowed_email(email: str, allowed_domains: Iterable[str]) -> bool:
    """Whether an address belongs to the institution.

    A configured domain admits itself and its subdomains, so `unipd.it` covers
    `studenti.unipd.it` and `phd.unipd.it` without listing them. The suffix
    test is on a dot boundary on purpose: `notunipd.it` must not pass because
    it happens to end in the domain, and `unipd.it.evil.example` must not pass
    because it happens to start with it.

    An empty allowlist admits nobody - a deleted line in `cocco.toml` should
    lock the door, not open it.
    """
    domain = email_domain(email)
    if not domain:
        return False
    return any(
        domain == allowed or domain.endswith("." + allowed)
        for allowed in allowed_domains
    )


########################################################################
# pure helpers


def normalize_email(raw: str) -> str:
    """The canonical form of an address, or `ValueError`.

    Canonicalization, not validation: the address arrives in Google's `email`
    claim and is already well-formed. Case is folded, which is lossy for the
    local part in theory and irrelevant in practice - every address here is an
    institutional one.
    """
    candidate = raw.strip().lower()
    if not candidate:
        raise ValueError("an email address is required")
    if len(candidate) > MAX_EMAIL_LENGTH:
        raise ValueError("that email address is too long")
    local, at, domain = candidate.partition("@")
    if not at or not local or not domain or "@" in domain or domain.strip(".") == "":
        raise ValueError("that is not a usable email address")
    return candidate


def nickname_from_email(email: str) -> str:
    """The display name a brand-new account starts with: the local part."""
    return email.split("@", 1)[0][:64] or email[:64]


def google_redirect_uri(base_url: str) -> str:
    """Where Google sends the browser back.

    Must equal an Authorized redirect URI in the Google Cloud console character
    for character - which is why it carries no query string, and why the
    post-login destination travels in the session instead.
    """
    return f"{base_url.rstrip('/')}{GOOGLE_CALLBACK_PATH}"


def safe_next_path(raw: str | None) -> str:
    """A post-login destination that cannot be turned into an open redirect.

    Only a path on this site survives: it must start with a single `/`, and
    carry neither a scheme nor an authority. Anything else becomes the root.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/"
    return raw


########################################################################
# the account behind a confirmed address


async def get_or_create_user(conn: AsyncConnection, email: str) -> schema.User:
    """The account for an address, created on first sight. Does not commit.

    Two tabs finishing a first sign-in at the same moment both reach the
    insert; `user.email` is unique, so the loser catches the violation and
    re-reads the row the winner wrote.
    """
    row = await query_user_by_email(conn, email)
    if row is not None:
        return schema.User.model_validate(row)

    nickname = nickname_from_email(email)
    image, image_mime = generate_identicon(nickname)
    try:
        async with conn.begin_nested():
            user_id = await insert_user(conn, email, nickname, image, image_mime)
    except IntegrityError:
        row = await query_user_by_email(conn, email)
        if row is None:
            raise
        return schema.User.model_validate(row)
    # Not blocked, and that is the truth rather than a default: the row was
    # inserted a line ago, and nothing has had the chance to shut it out.
    return schema.User(
        id=user_id,
        email=email,
        nickname=nickname,
        blocked_at=None,
        blocked_reason=None,
    )
