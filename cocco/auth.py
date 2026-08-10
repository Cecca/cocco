"""Magic-link authentication: configuration, token helpers and the mailer.

Everything here is testable without a database or a running app, except
`get_or_create_user`, which is the one place the flow touches the schema.
"""

import logging
import os
import secrets
import smtplib
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlparse

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.avatar import generate_identicon
from cocco.repository import insert_user, query_user_by_email

logger = logging.getLogger(__name__)

# A link that sits in a mailbox is a credential, so it does not sit there long.
TOKEN_TTL = timedelta(minutes=15)

# RFC 5321's ceiling. Checked before the address is normalized, hashed or
# stored, so a megabyte in the form field costs nothing.
MAX_EMAIL_LENGTH = 254

# The rate limit on requesting a link, per address and per client. Generous
# enough that a student who mistypes twice is not locked out, small enough that
# the login form is not a mail cannon.
RATE_WINDOW = timedelta(hours=1)
MAX_LINKS_PER_ADDRESS = 5
MAX_LINKS_PER_CLIENT = 20


########################################################################
# configuration


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    from_address: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    from_name: str = "COCCO"
    timeout: float = 10.0


@dataclass(frozen=True)
class SessionConfig:
    secret_key: str
    https_only: bool = False
    max_age: int = 14 * 24 * 3600
    cookie_name: str = "cocco_session"


@dataclass(frozen=True)
class AppConfig:
    session: SessionConfig
    # None means SMTP is unconfigured, which is not an error: the link is
    # logged instead of mailed, so a fresh checkout can log in.
    smtp: SmtpConfig | None
    # Absolute base for the links that go into emails. None derives it from the
    # incoming request, which is only right behind a proxy that sets
    # X-Forwarded-* and a uvicorn running with --proxy-headers.
    base_url: str | None = None


def load_config(path: Path) -> AppConfig:
    """Read `cocco.toml`, in the shape `load_languages` reads `languages.toml`.

    A missing file is not an error - a development checkout has none and must
    still boot. Neither is an `[smtp]` table too incomplete to send with: both
    fall back to logging the magic link.
    """
    data: dict = {}
    if path.is_file():
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    else:
        logger.info("no configuration file at %s; using defaults", path)

    return AppConfig(
        session=_load_session_config(data.get("session", {})),
        smtp=_load_smtp_config(data.get("smtp", {})),
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


def _load_smtp_config(section: dict) -> SmtpConfig | None:
    host = section.get("host")
    from_address = section.get("from_address")
    if not host or not from_address:
        if section:
            logger.warning(
                "[smtp] needs both `host` and `from_address`; "
                "magic links will be logged instead of mailed"
            )
        return None
    return SmtpConfig(
        host=host,
        from_address=from_address,
        port=int(section.get("port", SmtpConfig.port)),
        username=section.get("username"),
        password=section.get("password"),
        starttls=bool(section.get("starttls", True)),
        from_name=section.get("from_name", SmtpConfig.from_name),
        timeout=float(section.get("timeout", SmtpConfig.timeout)),
    )


########################################################################
# pure helpers


def normalize_email(raw: str) -> str:
    """The canonical form of an address, or `ValueError`.

    Case is folded, which is lossy for the local part in theory and irrelevant
    in practice: every address here is an institutional one.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("an email address is required")
    if len(candidate) > MAX_EMAIL_LENGTH:
        raise ValueError("that email address is too long")
    try:
        validated = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError as err:
        raise ValueError(str(err)) from err
    return validated.normalized.lower()


def nickname_from_email(email: str) -> str:
    """The display name a brand-new account starts with: the local part."""
    return email.split("@", 1)[0][:64] or email[:64]


def generate_token() -> str:
    """A bearer token for one magic link. 256 bits, so guessing is not a threat
    model and a plain hash is the right way to store it."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_expiry(now: datetime) -> datetime:
    return now + TOKEN_TTL


def is_expired(expires_at: datetime, now: datetime) -> bool:
    return expires_at <= now


def build_magic_link(base_url: str, token: str) -> str:
    # safe="" so nothing at all survives unescaped: `token_urlsafe` never emits
    # a `/`, but the link must not depend on that.
    return f"{base_url.rstrip('/')}/auth/verify?token={quote(token, safe='')}"


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
# sending


def format_magic_link_email(
    config: SmtpConfig, to: str, link: str, ttl: timedelta = TOKEN_TTL
) -> EmailMessage:
    """The message itself, built but not sent, so the wording and the headers
    are unit-testable without smtplib in the way."""
    minutes = int(ttl.total_seconds() // 60)
    message = EmailMessage()
    message["Subject"] = "Your COCCO sign-in link"
    message["From"] = f"{config.from_name} <{config.from_address}>"
    message["To"] = to
    message.set_content(
        "Hello,\n\n"
        "Use the link below to sign in to COCCO. It works once, and it expires "
        f"in {minutes} minutes.\n\n"
        f"{link}\n\n"
        "If you did not ask to sign in, you can ignore this message.\n"
    )
    return message


def send_magic_link(config: SmtpConfig | None, to: str, link: str) -> None:
    """Deliver the link, or log it when SMTP is unconfigured.

    A plain `def`, not `async def`: Starlette runs a sync background task in the
    threadpool, which is where blocking smtplib belongs. It never raises - the
    token has already been committed by the time this runs, so an SMTP outage
    must not become a 500 on a request that already succeeded.
    """
    if config is None:
        # The development path, and the only line anywhere that prints a live
        # token. Labelled loudly so it is obvious when it shows up in a real log.
        logger.warning("SMTP is not configured; magic link for %s: %s", to, link)
        return
    try:
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as smtp:
            if config.starttls:
                smtp.starttls()
            if config.username:
                smtp.login(config.username, config.password or "")
            smtp.send_message(format_magic_link_email(config, to, link))
    except Exception:
        logger.exception("could not send the magic link to %s", to)
    else:
        logger.info("sent a magic link to %s", to)


########################################################################
# the account behind a confirmed address


async def get_or_create_user(conn: AsyncConnection, email: str) -> schema.User:
    """The account for an address, created on first sight. Does not commit.

    Two links for the same new address, consumed at once, both reach the
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
    return schema.User(id=user_id, email=email, nickname=nickname)
