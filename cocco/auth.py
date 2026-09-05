"""Signing in: the domain allowlist and the account behind a confirmed
address.

Nothing here talks to a provider, and nothing here knows which one asked. The
OAuth clients and the routes live in `main.py`, where the request is; what is
left is pure and unit-testable, except `get_or_create_user`, which is the one
place the flow touches the schema - and which is keyed on the address alone, so
somebody who signs in with Google today and with GitHub tomorrow lands on the
same account by construction rather than by a rule anybody had to write.

The configuration is not here either - it is `cocco/config.py`, because the
judge reads it too and a database URL is none of sign-in's business.
"""

import logging
from collections.abc import Iterable
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.avatar import generate_identicon
from cocco.repository import insert_user, query_user_by_email

logger = logging.getLogger(__name__)

# Where a provider sends the browser back. A shape rather than a constant
# because there is more than one provider now, but it is still not something a
# route may invent per request: it must match, byte for byte, a redirect URI
# registered with the provider - an "Authorized redirect URI" in the Google
# Cloud console, an "Authorization callback URL" in a GitHub OAuth app.
OAUTH_CALLBACK_PATH = "/auth/{provider}/callback"

# RFC 5321's ceiling. Checked before the address is looked at any further, so a
# preposterous claim costs nothing.
MAX_EMAIL_LENGTH = 254


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


def oauth_redirect_uri(base_url: str, provider: str) -> str:
    """Where a provider sends the browser back.

    Must equal the redirect URI registered with that provider character for
    character - which is why it carries no query string, and why the
    post-login destination travels in the session instead. One function for
    both providers, so the two can only differ in the segment that names them.
    """
    return f"{base_url.rstrip('/')}{OAUTH_CALLBACK_PATH.format(provider=provider)}"


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
