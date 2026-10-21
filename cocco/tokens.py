"""Personal access tokens: the pure half.

Its own module for the reason `cocco/auth.py` is one - nothing here touches the
network, a request or the database, so the whole of it is testable by import.
The routes and the lookup live in `web/profile.py` and `repository/credentials.py`.

A token is the only credential in this app that is not a Google session, and it
exists because a command line has no browser to run OIDC in. It is therefore
treated as a password would be: generated from `secrets`, stored only as a
hash, and printed exactly once.
"""

import hashlib
import secrets

# A recognisable prefix, so a token pasted into a bug report or committed by
# mistake can be spotted by a human and by a secret scanner. It is part of the
# token: it is hashed with the rest and presented with it.
TOKEN_PREFIX = "cocco_pat_"

# 32 bytes of urlsafe base64. Well past guessing, and short enough to paste.
TOKEN_BYTES = 32


def mint_token() -> str:
    """A fresh token. The only place one is ever generated."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """What is stored, and what a presented token is looked up by.

    A plain SHA-256 rather than a password hash: the token is 256 bits of
    `secrets` output, not something a person chose, so there is no dictionary
    to slow an attacker down through and nothing for a work factor to buy.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_bearer(header: str | None) -> str | None:
    """The token out of an `Authorization` header, or None.

    Deliberately strict about the shape and deliberately lax about the case of
    the scheme, which RFC 7235 says is case-insensitive. Anything that is not
    exactly `Bearer <token>` - a `Basic`, a bare token, an empty header - is
    None rather than an error: the caller turns that into the one 401 it has,
    and telling an anonymous client which of its guesses was closer is not
    something to do.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None
