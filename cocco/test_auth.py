"""Tests for the pure half of the magic-link flow: nothing here needs a
database, an app or a mail server."""

import logging
from datetime import datetime, timedelta

import pytest

from cocco.auth import (
    MAX_EMAIL_LENGTH,
    SmtpConfig,
    build_magic_link,
    format_magic_link_email,
    generate_token,
    hash_token,
    is_expired,
    load_config,
    nickname_from_email,
    normalize_email,
    safe_next_path,
    send_magic_link,
)


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Alice@UNIPD.it \n") == "alice@unipd.it"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not an email", "a@b@c", "@unipd.it", "alice@", "a" * 300 + "@x.it"],
)
def test_normalize_email_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_email(raw)


def test_normalize_email_length_cap_precedes_validation():
    """The cap is checked before the address is parsed, so a huge form field
    costs nothing."""
    with pytest.raises(ValueError):
        normalize_email("a" * (MAX_EMAIL_LENGTH + 1) + "@unipd.it")


def test_nickname_from_email():
    assert nickname_from_email("matteo.ceccarello@unipd.it") == "matteo.ceccarello"


def test_generate_token_is_unique_and_hashes_stably():
    first, second = generate_token(), generate_token()
    assert first != second
    assert hash_token(first) == hash_token(first)
    assert hash_token(first) != hash_token(second)
    # The stored form must not be the token itself.
    assert hash_token(first) != first


def test_is_expired_at_the_boundary():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert is_expired(now, now)
    assert is_expired(now - timedelta(seconds=1), now)
    assert not is_expired(now + timedelta(seconds=1), now)


@pytest.mark.parametrize(
    "raw",
    [None, "", "//evil.example.org", "https://evil.example.org", "evil", "\\evil"],
)
def test_safe_next_path_refuses_to_leave_the_site(raw):
    assert safe_next_path(raw) == "/"


def test_safe_next_path_keeps_a_local_path():
    assert safe_next_path("/courses/1/2") == "/courses/1/2"


def test_build_magic_link_escapes_the_token():
    link = build_magic_link("https://cocco.example.org/", "a+b/c")
    assert link == "https://cocco.example.org/auth/verify?token=a%2Bb%2Fc"


def test_format_magic_link_email_carries_the_link():
    config = SmtpConfig(host="mail", from_address="cocco@unipd.it")
    message = format_magic_link_email(
        config, "bob@unipd.it", "https://x/auth/verify?token=t", timedelta(minutes=15)
    )
    assert message["To"] == "bob@unipd.it"
    assert message["From"] == "COCCO <cocco@unipd.it>"
    assert message["Subject"]
    body = message.get_content()
    assert "https://x/auth/verify?token=t" in body
    assert "15 minutes" in body


def test_send_magic_link_logs_when_smtp_is_unconfigured(caplog):
    with caplog.at_level(logging.WARNING, logger="cocco.auth"):
        send_magic_link(None, "bob@unipd.it", "https://x/auth/verify?token=t")
    assert "https://x/auth/verify?token=t" in caplog.text


########################################################################
# configuration


def test_load_config_without_a_file(tmp_path):
    config = load_config(tmp_path / "absent.toml")
    assert config.smtp is None
    assert config.base_url is None
    assert config.session.secret_key


def test_load_config_reads_smtp_and_site(tmp_path):
    path = tmp_path / "cocco.toml"
    path.write_text(
        """
        [session]
        secret_key = "s3cret"
        https_only = true

        [smtp]
        host = "smtp.unipd.it"
        from_address = "cocco@unipd.it"
        port = 25
        starttls = false

        [site]
        base_url = "https://cocco.example.org"
        """
    )
    config = load_config(path)
    assert config.session.secret_key == "s3cret"
    assert config.session.https_only
    assert config.smtp is not None
    assert (config.smtp.host, config.smtp.port, config.smtp.starttls) == (
        "smtp.unipd.it",
        25,
        False,
    )
    assert config.base_url == "https://cocco.example.org"


def test_load_config_ignores_an_incomplete_smtp_section(tmp_path):
    """A half-filled [smtp] falls back to logging the link rather than raising:
    a misconfigured mail server must not stop the app from booting."""
    path = tmp_path / "cocco.toml"
    path.write_text('[smtp]\nhost = "smtp.unipd.it"\n')
    assert load_config(path).smtp is None


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
