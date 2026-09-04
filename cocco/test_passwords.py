"""`cocco/passwords.py`, which is pure: no database, no app, no network.

The counterpart of `test_tokens.py` next door, and the tests that matter here
are the ones about *refusing* rather than the round trip - `verify_password` is
handed whatever a column contains, and a route depends on it never raising.
"""

import base64

import pytest

from cocco.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    SCHEME,
    SCRYPT_N,
    check_password,
    generate_password,
    hash_password,
    needs_rehash,
    verify_password,
)

GOOD = "correct horse battery staple"


# ---------------------------------------------------------------------------
# hashing and verifying


def test_a_password_verifies_against_its_own_hash():
    assert verify_password(GOOD, hash_password(GOOD))


def test_a_wrong_password_does_not():
    encoded = hash_password(GOOD)
    assert not verify_password(GOOD + "!", encoded)
    assert not verify_password("", encoded)
    assert not verify_password(GOOD.upper(), encoded)


def test_the_same_password_hashes_differently_every_time():
    """A salt per password, which is the whole reason this is not
    `hash_token`'s SHA-256: without it one dictionary run answers every row at
    once."""
    first, second = hash_password(GOOD), hash_password(GOOD)
    assert first != second
    assert verify_password(GOOD, first)
    assert verify_password(GOOD, second)


def test_the_encoding_carries_its_own_parameters():
    scheme, n, r, p, salt, key = hash_password(GOOD).split("$")
    assert scheme == SCHEME
    assert (int(n), int(r), int(p)) == (SCRYPT_N, 8, 1)
    assert len(base64.b64decode(salt)) == 16
    assert len(base64.b64decode(key)) == 32


def test_a_hash_made_under_other_parameters_still_verifies():
    """The point of writing the parameters down: raising `SCRYPT_N` must not
    invalidate every password already stored."""
    encoded = hash_password(GOOD)
    scheme, _, r, p, salt, _ = encoded.split("$")
    # Re-derive at a cost this build does not use any more.
    import hashlib

    weak = hashlib.scrypt(
        GOOD.encode(), salt=base64.b64decode(salt), n=2**12, r=8, p=1, dklen=32
    )
    older = "$".join(
        [scheme, "4096", r, p, salt, base64.b64encode(weak).decode()]
    )
    assert verify_password(GOOD, older)
    assert needs_rehash(older)
    assert not needs_rehash(encoded)


# ---------------------------------------------------------------------------
# what a column might actually contain
#
# `query_password_hash` returns None both for an address nobody has and for an
# account with no password, and the sign-in route hands that straight here. A
# raise on any of these would be a 500 on the login page.


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        "",
        "not a hash at all",
        "scrypt$",
        "scrypt$16384$8$1$only-four-parts",
        "scrypt$16384$8$1$c2FsdA==$a2V5$extra",
        "bcrypt$16384$8$1$c2FsdA==$a2V5",
        "scrypt$notanumber$8$1$c2FsdA==$a2V5",
        "scrypt$16384$8$1$!!!not-base64!!!$a2V5",
        "scrypt$0$8$1$c2FsdA==$a2V5",
        "scrypt$16384$8$1$$a2V5",
    ],
)
def test_a_hash_that_is_not_one_is_false_and_never_an_error(encoded):
    assert verify_password(GOOD, encoded) is False


def test_absurd_parameters_are_refused_rather_than_allocated():
    """A row must not get to decide how much memory a request may take."""
    salt = base64.b64encode(b"0123456789abcdef").decode()
    key = base64.b64encode(b"0" * 32).decode()
    assert not verify_password(GOOD, f"scrypt$1073741824$8$1${salt}${key}")


def test_needs_rehash_on_nothing_is_false():
    """Asked after a successful sign-in, so None never reaches it in practice -
    but a False here is the answer that does nothing."""
    assert needs_rehash(None) is False


# ---------------------------------------------------------------------------
# the strength rule


def test_a_long_enough_password_is_accepted():
    check_password("x" * MIN_PASSWORD_LENGTH)


@pytest.mark.parametrize(
    "password, fragment",
    [
        ("", "required"),
        ("x" * (MIN_PASSWORD_LENGTH - 1), str(MIN_PASSWORD_LENGTH)),
        ("x" * (MAX_PASSWORD_LENGTH + 1), "too long"),
    ],
)
def test_a_password_that_will_not_do_says_why(password, fragment):
    with pytest.raises(ValueError) as err:
        check_password(password)
    assert fragment in str(err.value)


# ---------------------------------------------------------------------------
# an issued password


def test_an_issued_password_passes_the_rule_it_will_be_checked_against():
    """`/admin` issues one and its owner replaces it from their profile; a
    generated password that the change form would then reject would be a trap
    with nobody to blame for it."""
    issued = generate_password()
    check_password(issued)
    assert verify_password(issued, hash_password(issued))


def test_two_issued_passwords_differ():
    assert generate_password() != generate_password()
