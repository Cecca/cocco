"""Tests for what cocco is configured with: nothing here needs a database, an
app or a network.

The property under most of them is the split the module exists to make - the
file is settings, the environment is secrets - and the two halves of it are
"a secret written in the file is ignored, and said so" and "a setting left in
the environment is ignored, and said so"."""

from pathlib import Path

import pytest

from cocco.config import (
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_DATABASE_URL,
    DEFAULT_FLAKE_REF,
    DEFAULT_STORAGE_ROOT,
    config_path,
    load_config,
)

SECRET_VARIABLES = (
    "COCCO_SECRET_KEY",
    "COCCO_GOOGLE_CLIENT_ID",
    "COCCO_GOOGLE_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def no_secrets_in_the_environment(monkeypatch):
    """The secrets now have exactly one source, so every test here has to say
    what is in it. A developer's shell - or a `.env` somebody has sourced -
    would otherwise decide the outcome of half of these."""
    for variable in SECRET_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


########################################################################
# the defaults, and the file


def test_load_config_without_a_file(tmp_path):
    """A checkout that has not been configured at all must still boot - and
    boot safe."""
    config = load_config(tmp_path / "absent.toml")
    assert config.auth.google is None
    assert config.auth.allowed_domains == DEFAULT_ALLOWED_DOMAINS
    assert config.base_url is None
    assert config.session.secret_key
    # No administrators is the ordinary case, not a hole: an existing database
    # already has its own, and the bootstrap only ever adds.
    assert config.auth.administrators == ()
    assert config.database.url == DEFAULT_DATABASE_URL
    assert config.storage.root == DEFAULT_STORAGE_ROOT
    assert config.judge.flake == DEFAULT_FLAKE_REF
    assert config.dev.dummy_auth is False
    assert config.dev.reset_db is False


def test_load_config_reads_the_settings(tmp_path):
    path = tmp_path / "cocco.toml"
    path.write_text(
        """
        [session]
        https_only = true

        [auth]
        allowed_domains = ["unipd.it", "Example.ORG"]

        [site]
        base_url = "https://cocco.example.org"
        """
    )
    config = load_config(path)
    assert config.session.https_only
    assert config.auth.allowed_domains == ("unipd.it", "example.org")
    assert config.base_url == "https://cocco.example.org"


def test_config_path_names_the_file_and_only_that(monkeypatch):
    monkeypatch.delenv("COCCO_CONFIG", raising=False)
    assert config_path() == Path("cocco.toml")
    monkeypatch.setenv("COCCO_CONFIG", "elsewhere/cocco.dev.toml")
    assert config_path() == Path("elsewhere/cocco.dev.toml")


########################################################################
# the secrets come from the environment, and only from there


def test_the_google_credentials_come_from_the_environment_alone(tmp_path, caplog):
    """Written in the file they do nothing - and they are said out loud, or a
    deployment that had them there would lose Google sign-in in silence."""
    path = tmp_path / "cocco.toml"
    path.write_text(
        '[auth]\ngoogle_client_id = "from-file"\ngoogle_client_secret = "from-file"\n'
    )
    with caplog.at_level("WARNING", logger="cocco.config"):
        config = load_config(path)
    assert config.auth.google is None
    assert "COCCO_GOOGLE_CLIENT_ID" in caplog.text
    assert "COCCO_GOOGLE_CLIENT_SECRET" in caplog.text


def test_the_google_client_is_read_from_the_environment(tmp_path, monkeypatch):
    """The credentials from the environment, the settings around them from the
    file: `google_hd` is a hint for the account chooser, not a secret."""
    path = tmp_path / "cocco.toml"
    path.write_text('[auth]\ngoogle_hd = "unipd.it"\n')
    monkeypatch.setenv("COCCO_GOOGLE_CLIENT_ID", "an-id")
    monkeypatch.setenv("COCCO_GOOGLE_CLIENT_SECRET", "a-secret")
    google = load_config(path).auth.google
    assert google is not None
    assert (google.client_id, google.client_secret) == ("an-id", "a-secret")
    assert google.hd == "unipd.it"


def test_half_a_credential_is_no_credential(tmp_path, monkeypatch, caplog):
    """A typo must not stop the app from booting: it warns, and Google sign-in
    is simply off."""
    monkeypatch.setenv("COCCO_GOOGLE_CLIENT_ID", "an-id")
    path = tmp_path / "absent.toml"
    with caplog.at_level("WARNING", logger="cocco.config"):
        config = load_config(path)
    assert config.auth.google is None
    assert "COCCO_GOOGLE_CLIENT_SECRET" in caplog.text


def test_a_secret_key_in_the_file_is_ignored_and_said_so(tmp_path, caplog):
    path = tmp_path / "cocco.toml"
    path.write_text('[session]\nsecret_key = "from-file"\n')
    with caplog.at_level("WARNING", logger="cocco.config"):
        config = load_config(path)
    assert config.session.secret_key != "from-file"
    assert "COCCO_SECRET_KEY" in caplog.text


def test_the_secret_key_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COCCO_SECRET_KEY", "from-env")
    assert load_config(tmp_path / "absent.toml").session.secret_key == "from-env"


def test_an_unconfigured_secret_is_never_a_fixed_default(tmp_path):
    """The property that matters: there is no constant in the tree to leak. An
    unconfigured deployment logs everyone out on restart, which is loud."""
    path = tmp_path / "absent.toml"
    assert load_config(path).session.secret_key != load_config(path).session.secret_key


########################################################################
# the database, the storage root and the development shortcuts


def test_the_database_url_comes_from_the_file(tmp_path):
    path = tmp_path / "cocco.toml"
    path.write_text('[database]\nurl = "postgresql+psycopg://cocco@db/cocco"\n')
    assert load_config(path).database.url == "postgresql+psycopg://cocco@db/cocco"


@pytest.mark.parametrize("body", ["", "[database]\n", '[database]\nurl = ""\n'])
def test_an_unset_database_url_defers_to_libpq(tmp_path, body):
    """The empty URL is the default rather than a guess at localhost: libpq
    resolves it from PGUSER/PGDATABASE/PGHOST, which is how the devShell's
    socket-only Postgres is reached without a `$PWD` written into a file."""
    path = tmp_path / "cocco.toml"
    path.write_text(body)
    assert load_config(path).database.url == DEFAULT_DATABASE_URL


def test_the_storage_root_arrives_as_a_path(tmp_path):
    """`Storage` is constructed straight from it."""
    path = tmp_path / "cocco.toml"
    path.write_text('[storage]\nroot = "/srv/cocco"\n')
    assert load_config(path).storage.root == Path("/srv/cocco")


def test_the_judge_flake_is_read(tmp_path):
    """The only setting the web app never looks at: which flake the judge
    builds its language runtimes out of."""
    path = tmp_path / "cocco.toml"
    path.write_text('[judge]\nflake = "/opt/cocco"\n')
    assert load_config(path).judge.flake == "/opt/cocco"


@pytest.mark.parametrize("body", ["", "[judge]\n", '[judge]\nflake = ""\n'])
def test_the_judge_flake_defaults_to_the_working_directory(tmp_path, body):
    """"." is what the ref was implicitly before it could be written down, so
    a file that says nothing about it leaves `just dev` exactly as it was. An
    explicit empty string is a typo, not a request for `nix build #python3`."""
    path = tmp_path / "cocco.toml"
    path.write_text(body)
    assert load_config(path).judge.flake == DEFAULT_FLAKE_REF


def test_the_judge_gcroots_are_read(tmp_path):
    """Where the judge writes the symlinks that hold its language runtimes
    against `nix-store --gc`. A `Path`, because `build_runtime` joins onto it."""
    path = tmp_path / "cocco.toml"
    path.write_text('[judge]\ngcroots = "/var/lib/cocco/gcroots"\n')
    assert load_config(path).judge.gcroots == Path("/var/lib/cocco/gcroots")


@pytest.mark.parametrize("body", ["", "[judge]\n", '[judge]\ngcroots = ""\n'])
def test_the_judge_gcroots_default_under_xdg_state_home(tmp_path, monkeypatch, body):
    """Nothing to configure for `just dev` or for an ordinary service: the
    default is durable, per user and per host, which is exactly the scope a GC
    root has. An explicit empty string is a typo, not a request to scatter
    symlinks over the filesystem root."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "cocco.toml"
    path.write_text(body)
    assert load_config(path).judge.gcroots == tmp_path / "state" / "cocco" / "gcroots"


def test_the_judge_gcroots_fall_back_to_the_home_state_directory(tmp_path, monkeypatch):
    """`$XDG_STATE_HOME` is frequently unset, and the specification's own
    fallback is what a systemd service with a real user gets."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "cocco.toml"
    path.write_text("")
    expected = tmp_path / ".local" / "state" / "cocco" / "gcroots"
    assert load_config(path).judge.gcroots == expected


def test_the_dev_table_is_read(tmp_path):
    path = tmp_path / "cocco.toml"
    path.write_text(
        "[dev]\ndummy_auth = true\nreset_db = true\n"
    )
    dev = load_config(path).dev
    assert dev.dummy_auth is True
    assert dev.reset_db is True


def test_dev_is_off_unless_the_file_says_so(tmp_path):
    """The property `[dev]` was made one table for: a configured deployment
    that never mentions it cannot have any of them on."""
    path = tmp_path / "cocco.toml"
    path.write_text(
        '[database]\nurl = "postgresql+psycopg://cocco@db/cocco"\n'
        '[storage]\nroot = "/srv/cocco"\n'
    )
    dev = load_config(path).dev
    assert dev.dummy_auth is False
    assert dev.reset_db is False


########################################################################
# the allowlist and the bootstrap administrators


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
    with caplog.at_level("WARNING", logger="cocco.config"):
        config = load_config(path)
    assert config.auth.administrators == ("someone@example.org",)
    assert "allowed_domains" in caplog.text


########################################################################
# migration


@pytest.mark.parametrize(
    "variable,replacement",
    [
        ("COCCO_DUMMY_AUTH", "dummy_auth"),
        ("COCCO_RESET_DB", "reset_db"),
        ("COCCO_STORAGE", "root"),
    ],
)
def test_a_setting_left_in_the_environment_is_named_out_loud(
    tmp_path, monkeypatch, caplog, variable, replacement
):
    """COCCO_DUMMY_AUTH=0 is the dangerous one: it is how somebody turns the
    shortcut *off* to exercise the real sign-in flow, so going quiet would
    leave them in the one configuration they meant not to be in."""
    monkeypatch.setenv(variable, "0")
    with caplog.at_level("WARNING", logger="cocco.config"):
        load_config(tmp_path / "absent.toml")
    assert variable in caplog.text
    assert replacement in caplog.text


def test_dummy_user_is_named_out_loud_in_both_of_its_spellings(
    tmp_path, monkeypatch, caplog
):
    """The third kind of removed setting: it moved nowhere, it was deleted.
    Somebody with either spelling left behind is expecting to be signed in as
    one particular person, and going quiet would let them browse as whoever the
    box last minted a session for."""
    path = tmp_path / "cocco.toml"
    path.write_text('[dev]\ndummy_auth = true\ndummy_user = "alice@unipd.it"\n')
    monkeypatch.setenv("COCCO_DUMMY_USER", "bob@unipd.it")

    with caplog.at_level("WARNING", logger="cocco.config"):
        config = load_config(path)

    assert "COCCO_DUMMY_USER" in caplog.text
    assert "[dev] dummy_user" in caplog.text
    assert "/login asks" in caplog.text
    # Warned about, never applied - and the rest of `[dev]` is still read.
    assert config.dev.dummy_auth is True


def test_the_postgres_variables_are_not_warned_about(tmp_path, monkeypatch, caplog):
    """They are libpq's own and are still read, on purpose - they are the whole
    mechanism behind an empty database URL."""
    monkeypatch.setenv("PGHOST", "/somewhere/.pg/sock")
    monkeypatch.setenv("PGUSER", "postgres")
    monkeypatch.setenv("PGDATABASE", "cocco-dev")
    with caplog.at_level("WARNING", logger="cocco.config"):
        load_config(tmp_path / "absent.toml")
    assert "PGHOST" not in caplog.text


########################################################################
# the file that is in git


def test_the_committed_dev_config_loads():
    """`cocco.dev.toml` is tracked and `just dev` depends on it, so it is
    parsed by a test rather than left to rot - and the assertion that it
    configures no Google client is the guard on what makes committing it
    possible at all: there is no secret in it.

    Located from this file rather than from the working directory: it is one
    of the few things that really does live in the checkout and not in the
    package, and `load_config` treats a missing file as "use the defaults" -
    so a relative path would not fail here, it would quietly assert about
    something else."""
    config = load_config(Path(__file__).resolve().parent.parent / "cocco.dev.toml")
    assert config.dev.dummy_auth is True
    assert config.dev.reset_db is False
    assert config.auth.google is None
    assert config.database.url == DEFAULT_DATABASE_URL
