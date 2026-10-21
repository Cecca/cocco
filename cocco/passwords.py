"""Passwords: the pure half.

Its own module for the reason `cocco/auth.py` and `cocco/tokens.py` are ones -
nothing here touches the network, a request or the database, so the whole of it
is testable by import. The routes live in `web/signin.py` and `web/profile.py`, the two statements in
`repository/credentials.py`, and the switch that decides whether any of it is reachable is
`[auth] password_login` in `cocco/config.py`.

This is `tokens.py`'s deliberate opposite, and `hash_token`'s docstring already
says why: a token is 256 bits of `secrets` output, so a plain SHA-256 is right
for it and a work factor would buy nothing. A password is something a person
chose out of a very small space, so it needs the opposite treatment - a salt
per password, so one dictionary cannot be run against every row at once, and a
work factor, so the dictionary is slow.

The work factor is scrypt, from the standard library. Nothing is added to
`pyproject.toml` for it, which matters more here than it looks: a new
dependency would have to be built by the Nix devShell and shipped in the wheel,
and `hashlib` is already linked against the OpenSSL that provides it.
"""

import base64
import hashlib
import hmac
import secrets

# The parameters. `n` is the cost, and the memory scrypt uses is `128 * n * r`
# - 32 MiB here, per concurrent verification, which is the point of scrypt and
# the reason a rented GPU is not much better at this than the judge host is.
#
# `maxmem` is not a tuning knob but a trap: `hashlib.scrypt` defaults it to
# 32 MiB and raises when the parameters need more, which these do once the
# allocator's overhead is counted. It has to be passed, and passing it is what
# lets `SCRYPT_N` be raised later without the change failing at run time.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024

# 16 bytes of salt and a 32 byte derived key, both from `secrets`.
SALT_BYTES = 16
KEY_BYTES = 32

# The rule, and it is a length and nothing else. A composition rule - a digit,
# a capital, a symbol - shrinks the space people actually pick from rather than
# growing it, and this app cannot check a password against a breach list
# because it has no network in it.
MIN_PASSWORD_LENGTH = 12

# Not a security limit but a cost one: scrypt hashes its input first, so a long
# password is not slower, but nothing about a megabyte in a form field is a
# person signing in.
MAX_PASSWORD_LENGTH = 1024

# What an issued password is worth, in bytes of `secrets` before base64. Long
# enough that it needs no work factor of its own, short enough to be read out
# over a desk - which is exactly what happens to it.
ISSUED_PASSWORD_BYTES = 12

# The algorithm label the encoding starts with. It is written down so that
# `verify_password` can refuse anything it does not recognise rather than
# guessing, and so a second algorithm could be added beside this one later.
SCHEME = "scrypt"


def check_password(password: str) -> None:
    """Raise `ValueError` with a sentence if this is not a usable password.

    A sentence and not a bool, in `normalize_email`'s style: every caller has
    somewhere to print it, and the ones that do not would have had to invent
    the wording themselves.
    """
    if not password:
        raise ValueError("a password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"a password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("that password is too long")


def generate_password() -> str:
    """A fresh password, for an administrator to issue and read out.

    The counterpart of `mint_token`, and the same discipline: generated from
    `secrets`, shown exactly once, and nowhere readable afterwards. Its owner
    is expected to replace it from their profile page.
    """
    return secrets.token_urlsafe(ISSUED_PASSWORD_BYTES)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_BYTES,
        maxmem=SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    """What is stored: `scrypt$n$r$p$<salt>$<key>`, base64 in the two halves.

    Self-describing on purpose. The parameters travel with the hash rather than
    being read from the constants above at verification time, so raising
    `SCRYPT_N` next year leaves every existing password working - each is
    verified with the parameters it was made under, and is re-made under the
    current ones the next time its owner types it.

    Synchronous and, by design, slow: every caller in an async route runs it
    through `starlette.concurrency.run_in_threadpool`, because 32 MiB of
    sequential work on the event loop would stop the whole app.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(key).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    """Whether `password` is the one `encoded` was made from.

    Takes `str | None` and never raises, which is what lets the sign-in route
    have one code path: an address nobody has ever registered and an account
    that has a row but no password both arrive here as None, and both take the
    same time and produce the same sentence as a wrong password would. A route
    that short-circuited on None would answer faster for an address that does
    not exist, and that is a user list.

    A malformed encoding - a truncated column, a hash written by some future
    scheme this build does not know - is False for the same reason.
    """
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return False
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return False
    if not salt or not expected or n < 2 or r < 1 or p < 1:
        return False
    try:
        candidate = _derive(password, salt, n, r, p)
    except (ValueError, MemoryError):
        # Parameters this build will not spend the memory on. Refusing is the
        # only safe answer: the alternative is letting a row decide how much
        # memory a request may allocate.
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(encoded: str | None) -> bool:
    """Whether a stored hash was made under parameters we have since raised.

    Asked right after a successful sign-in, which is the one moment the plain
    password is in hand and re-hashing costs nothing extra.
    """
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return True
    try:
        n, r, p = (int(part) for part in parts[1:4])
    except ValueError:
        return True
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
