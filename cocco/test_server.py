"""`cocco-web`'s hand-off to uvicorn, checked on the arguments it passes.

Nothing is served: `uvicorn.run` is replaced by a function that records its
keyword arguments, and the two startup refusals by no-ops, so these need no
database. `configure_logging` is replaced too, because applying it for real
would take pytest's `caplog` handler off the root logger for every test that
runs after these.
"""

import pytest

import cocco.logs
import cocco.server as server


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Run `cocco-web` against a file, and return what uvicorn was given."""
    calls = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr(server, "_check_schema", lambda config: None)
    monkeypatch.setattr(server, "_check_storage", lambda config: None)
    monkeypatch.setattr(cocco.logs, "configure_logging", lambda *a, **kw: None)
    # `main` sets COCCO_CONFIG from --config; monkeypatch puts it back.
    monkeypatch.setenv("COCCO_CONFIG", str(tmp_path / "absent.toml"))

    def run(body: str, *argv: str) -> dict:
        path = tmp_path / "cocco.toml"
        path.write_text(body)
        server.main(["--config", str(path), *argv])
        [kwargs] = calls
        return kwargs

    return run


@pytest.mark.parametrize("how", [["--reload"], ["--workers", "2"], []])
def test_uvicorn_is_given_the_logging_config_built_from_the_file(served, how):
    """`log_config` is the only form that reaches the child processes uvicorn
    starts under --reload and --workers, so that is what has to carry it."""
    kwargs = served('[log]\nformat = "json"\nlevel = "warning"\n', *how)
    config = kwargs["log_config"]
    assert config["formatters"]["cocco"]["()"] == "cocco.logs.JsonFormatter"
    assert config["formatters"]["cocco"]["process"] == "web"
    assert config["root"]["level"] == "WARNING"
    # uvicorn's own access line is replaced by `cocco.access`, and its
    # `log_level` would override the dict's levels.
    assert kwargs["access_log"] is False
    assert kwargs["log_level"] is None


def test_log_level_on_the_command_line_overrides_the_file(served):
    kwargs = served('[log]\nlevel = "WARNING"\n', "--log-level", "DEBUG")
    assert kwargs["log_config"]["root"]["level"] == "DEBUG"


def test_the_trusted_proxies_come_from_the_file_and_not_the_environment(
    served, monkeypatch
):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    kwargs = served('[site]\ntrusted_proxies = ["10.1.2.3"]\n')
    assert kwargs["forwarded_allow_ips"] == ["10.1.2.3"]
    # Without the deprecated flag uvicorn rewrites nothing: the app does it.
    assert kwargs["proxy_headers"] is False


def test_the_proxy_headers_flag_still_works_and_says_it_is_deprecated(
    served, caplog
):
    with caplog.at_level("WARNING", logger="cocco.server"):
        kwargs = served("", "--proxy-headers")
    assert kwargs["proxy_headers"] is True
    assert "[site] proxy_headers" in caplog.text


def test_a_log_table_that_cannot_be_read_refuses_to_start(served, capsys):
    with pytest.raises(SystemExit) as exit_code:
        served('[log]\nformat = "xml"\n')
    assert exit_code.value.code == 1
    assert "[log] format" in capsys.readouterr().err
