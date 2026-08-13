"""The token helpers. No database, no request - see `cocco/tokens.py`."""

import pytest

from cocco.tokens import TOKEN_PREFIX, hash_token, mint_token, parse_bearer


def test_a_token_is_recognisable_and_unguessable():
    token = mint_token()
    assert token.startswith(TOKEN_PREFIX)
    # The prefix is there so a leaked token can be spotted by eye; the rest is
    # what makes it a credential rather than a name.
    assert len(token) > len(TOKEN_PREFIX) + 32
    assert mint_token() != token


def test_the_hash_is_stable_and_is_not_the_token():
    token = mint_token()
    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)
    assert hash_token(token) != hash_token(mint_token())


def test_the_prefix_is_part_of_what_is_hashed():
    """It travels with the token, so it has to be part of the lookup key -
    otherwise a token presented without it would still be accepted."""
    token = mint_token()
    assert hash_token(token) != hash_token(token.removeprefix(TOKEN_PREFIX))


@pytest.mark.parametrize(
    "header, expected",
    [
        ("Bearer abc", "abc"),
        # RFC 7235 says the scheme is case-insensitive, and clients differ.
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("Bearer   abc  ", "abc"),
        # Everything else is one miss, not a family of distinguishable ones:
        # telling an anonymous caller which guess was closer is not something
        # to do.
        ("Basic abc", None),
        ("abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_bearer(header, expected):
    assert parse_bearer(header) == expected
