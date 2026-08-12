"""Tests for the pure half of Google sign-in: nothing here needs a database,
an app or a network."""

import pytest

from cocco.auth import (
    DEFAULT_ALLOWED_DOMAINS,
    MAX_EMAIL_LENGTH,
    email_domain,
    google_redirect_uri,
    is_allowed_email,
    load_config,
    nickname_from_email,
    normalize_domains,
    normalize_email,
    safe_next_path,
)


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


########################################################################
# configuration


def test_load_config_without_a_file(tmp_path):
    config = load_config(tmp_path / "absent.toml")
    assert config.auth.google is None
    assert config.auth.allowed_domains == DEFAULT_ALLOWED_DOMAINS
    assert config.base_url is None
    assert config.session.secret_key
    # No administrators is the ordinary case, not a hole: an existing database
    # already has its own, and the bootstrap only ever adds.
    assert config.auth.administrators == ()


def test_load_config_reads_the_auth_table(tmp_path, monkeypatch):
    monkeypatch.delenv("COCCO_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("COCCO_GOOGLE_CLIENT_SECRET", raising=False)
    path = tmp_path / "cocco.toml"
    path.write_text(
        """
        [session]
        secret_key = "s3cret"
        https_only = true

        [auth]
        allowed_domains = ["unipd.it", "Example.ORG"]
        google_client_id = "an-id"
        google_client_secret = "a-secret"
        google_hd = "unipd.it"

        [site]
        base_url = "https://cocco.example.org"
        """
    )
    config = load_config(path)
    assert config.session.secret_key == "s3cret"
    assert config.session.https_only
    assert config.auth.allowed_domains == ("unipd.it", "example.org")
    assert config.auth.google is not None
    assert config.auth.google.client_id == "an-id"
    assert config.auth.google.client_secret == "a-secret"
    assert config.auth.google.hd == "unipd.it"
    assert config.base_url == "https://cocco.example.org"


def test_half_a_credential_is_no_credential(tmp_path, monkeypatch, caplog):
    """A typo must not stop the app from booting: it warns, and Google sign-in
    is simply off."""
    monkeypatch.delenv("COCCO_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("COCCO_GOOGLE_CLIENT_SECRET", raising=False)
    path = tmp_path / "cocco.toml"
    path.write_text('[auth]\ngoogle_client_id = "an-id"\n')
    with caplog.at_level("WARNING", logger="cocco.auth"):
        config = load_config(path)
    assert config.auth.google is None
    assert "google_client_secret" in caplog.text


def test_the_environment_beats_the_file_for_the_google_client(tmp_path, monkeypatch):
    path = tmp_path / "cocco.toml"
    path.write_text(
        '[auth]\ngoogle_client_id = "from-file"\ngoogle_client_secret = "from-file"\n'
    )
    monkeypatch.setenv("COCCO_GOOGLE_CLIENT_ID", "from-env")
    monkeypatch.setenv("COCCO_GOOGLE_CLIENT_SECRET", "also-from-env")
    google = load_config(path).auth.google
    assert google is not None
    assert (google.client_id, google.client_secret) == ("from-env", "also-from-env")


def test_an_empty_allowed_domains_list_is_honoured(tmp_path):
    """Written explicitly, `[]` means nobody - it must not fall back to the
    default, or the safe misconfiguration would silently become the unsafe one."""
    path = tmp_path / "cocco.toml"
    path.write_text("[auth]\nallowed_domains = []\n")
    assert load_config(path).auth.allowed_domains == ()


def test_the_administrators_are_normalized_and_deduped(tmp_path):
    """They land in `administrator`, whose rows are compared against
    `user.email` - so they have to arrive in the same form that column holds."""
    path = tmp_path / "cocco.toml"
    path.write_text(
        '[auth]\nadministrators = [" Matteo@UNIPD.it ", "alice@unipd.it", '
        '"MATTEO@unipd.it"]\n'
    )
    assert load_config(path).auth.administrators == (
        "matteo@unipd.it",
        "alice@unipd.it",
    )


def test_one_bad_administrator_does_not_take_the_others_down(tmp_path, caplog):
    """A line that is not an address at all is dropped with a warning: the app
    must still boot, and the administrators around it are perfectly good."""
    path = tmp_path / "cocco.toml"
    path.write_text('[auth]\nadministrators = ["not-an-email", "alice@unipd.it"]\n')
    with caplog.at_level("WARNING", logger="cocco.auth"):
        config = load_config(path)
    assert config.auth.administrators == ("alice@unipd.it",)
    assert "not-an-email" in caplog.text


def test_an_administrator_who_could_never_sign_in_is_flagged(tmp_path, caplog):
    """Kept, because the table is the truth and the allowlist may be what is
    wrong - but said out loud, because it is almost always a typo."""
    path = tmp_path / "cocco.toml"
    path.write_text(
        '[auth]\nallowed_domains = ["unipd.it"]\n'
        'administrators = ["someone@example.org"]\n'
    )
    with caplog.at_level("WARNING", logger="cocco.auth"):
        config = load_config(path)
    assert config.auth.administrators == ("someone@example.org",)
    assert "allowed_domains" in caplog.text


def test_environment_beats_the_file_for_the_secret(tmp_path, monkeypatch):
    path = tmp_path / "cocco.toml"
    path.write_text('[session]\nsecret_key = "from-file"\n')
    monkeypatch.setenv("COCCO_SECRET_KEY", "from-env")
    assert load_config(path).session.secret_key == "from-env"


def test_an_unconfigured_secret_is_never_a_fixed_default(tmp_path, monkeypatch):
    """The property that matters: there is no constant in the tree to leak. An
    unconfigured deployment logs everyone out on restart, which is loud."""
    monkeypatch.delenv("COCCO_SECRET_KEY", raising=False)
    path = tmp_path / "absent.toml"
    assert load_config(path).session.secret_key != load_config(path).session.secret_key
