"""Tests for the pure half of Google sign-in: nothing here needs a database,
an app or a network."""

import pytest

from cocco.auth import (
    MAX_EMAIL_LENGTH,
    email_domain,
    google_redirect_uri,
    is_allowed_email,
    nickname_from_email,
    normalize_domains,
    normalize_email,
    safe_next_path,
)
from cocco.config import DEFAULT_ALLOWED_DOMAINS


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Alice@UNIPD.it \n") == "alice@unipd.it"


@pytest.mark.parametrize(
    "raw", ["", "   ", "not-an-email", "a@b@c", "@unipd.it", "alice@", "alice@."]
)
def test_normalize_email_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_email(raw)


def test_normalize_email_caps_the_length():
    with pytest.raises(ValueError):
        normalize_email("a" * (MAX_EMAIL_LENGTH + 1) + "@unipd.it")


def test_nickname_from_email():
    assert nickname_from_email("matteo.ceccarello@unipd.it") == "matteo.ceccarello"


@pytest.mark.parametrize(
    "raw",
    [None, "", "//evil.example.org", "https://evil.example.org", "evil", "\\evil"],
)
def test_safe_next_path_refuses_to_leave_the_site(raw):
    assert safe_next_path(raw) == "/"


def test_safe_next_path_keeps_a_local_path():
    assert safe_next_path("/courses/1/2") == "/courses/1/2"


def test_google_redirect_uri_carries_no_query_string():
    """It is matched character for character against the console registration,
    so a destination could never ride along in it."""
    assert (
        google_redirect_uri("https://cocco.example.org/")
        == "https://cocco.example.org/auth/google/callback"
    )
    assert google_redirect_uri("http://localhost:8000") == (
        "http://localhost:8000/auth/google/callback"
    )


########################################################################
# the domain allowlist


@pytest.mark.parametrize(
    "email",
    [
        "alice@unipd.it",
        "bob@studenti.unipd.it",
        "carol@phd.unipd.it",
        "dave@math.dei.unipd.it",
        "erin@UNIPD.IT",
        "frank@unipd.it.",  # a trailing root dot is still the same domain
    ],
)
def test_the_institution_and_its_subdomains_are_admitted(email):
    assert is_allowed_email(email, DEFAULT_ALLOWED_DOMAINS)


@pytest.mark.parametrize(
    "email",
    [
        "eve@gmail.com",
        "eve@notunipd.it",  # ends in the domain, but not on a dot boundary
        "eve@unipd.it.evil.example",  # starts with it, which is worse
        "eve@unipd.itx",
        "eve@it",
        "no-at-sign",
        "",
    ],
)
def test_everything_else_is_refused(email):
    assert not is_allowed_email(email, DEFAULT_ALLOWED_DOMAINS)


def test_an_empty_allowlist_admits_nobody():
    """A deleted line in cocco.toml must lock the door, not open it."""
    assert not is_allowed_email("alice@unipd.it", ())


def test_several_institutions_can_be_listed():
    domains = normalize_domains(["unipd.it", " .Example.ORG. ", "", "  "])
    assert domains == ("unipd.it", "example.org")
    assert is_allowed_email("a@sub.example.org", domains)
    assert is_allowed_email("a@unipd.it", domains)
    assert not is_allowed_email("a@elsewhere.it", domains)


def test_email_domain_takes_the_last_at():
    assert email_domain("a@b@unipd.it") == "unipd.it"
    assert email_domain("no-at-sign") == ""
