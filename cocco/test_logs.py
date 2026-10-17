"""Tests for the log format: nothing here needs a database, an app or a network.

Each test builds its own logger with its own handler writing into a buffer,
rather than calling `configure_logging`: that would replace the root handlers
pytest's `caplog` relies on, for every test collected after this one.
"""

import io
import json
import logging

import pytest

from cocco.config import LogConfig
from cocco.logs import ContextFilter, JsonFormatter, bind, logging_config


@pytest.fixture
def emit():
    """A logger whose output is returned, line by line, as parsed JSON."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter(process="test"))
    handler.addFilter(ContextFilter())
    logger = logging.getLogger("cocco.test_logs")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    def lines() -> list[dict]:
        return [json.loads(line) for line in buffer.getvalue().splitlines()]

    yield logger, lines
    logger.removeHandler(handler)


def test_one_record_is_one_parseable_line(emit):
    logger, lines = emit
    logger.info("graded %s in %.1fs", 812, 1.25)
    [line] = lines()
    assert line["msg"] == "graded 812 in 1.2s"
    assert line["level"] == "INFO"
    assert line["logger"] == "cocco.test_logs"
    assert line["process"] == "test"
    # ISO 8601, and UTC whatever the host's zone.
    assert line["ts"].endswith("+00:00")
    assert "exc" not in line


def test_context_fields_appear_and_disappear_with_bind(emit):
    logger, lines = emit
    with bind(request_id="abc"):
        logger.info("outer")
        with bind(user_id=7, ignored=None):
            logger.info("inner")
        logger.info("outer again")
    logger.info("outside")
    outer, inner, again, outside = lines()
    assert outer["request_id"] == "abc" and "user_id" not in outer
    assert inner["request_id"] == "abc" and inner["user_id"] == 7
    assert "ignored" not in inner
    assert "user_id" not in again
    assert "request_id" not in outside


def test_extra_fields_appear_and_win_over_the_context(emit):
    logger, lines = emit
    with bind(submission_id=1):
        logger.info("x", extra={"submission_id": 2, "elapsed_s": 0.5})
    [line] = lines()
    assert line["submission_id"] == 2
    assert line["elapsed_s"] == 0.5


def test_a_field_cannot_overwrite_a_fixed_key(emit):
    logger, lines = emit
    with bind(level="DEBUG"):
        logger.error("bad")
    [line] = lines()
    assert line["level"] == "ERROR"


def test_an_exception_is_serialised(emit):
    logger, lines = emit
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("it broke")
    [line] = lines()
    assert line["level"] == "ERROR"
    assert "RuntimeError: boom" in line["exc"]
    assert "Traceback" in line["exc"]


def test_a_value_json_cannot_represent_does_not_raise(emit):
    logger, lines = emit

    class Opaque:
        def __str__(self):
            return "opaque!"

    logger.info("x", extra={"thing": Opaque(), "items": {1, 2}})
    [line] = lines()
    assert line["thing"] == "opaque!"
    assert isinstance(line["items"], str)


def test_the_config_dict_builds_both_formats():
    """Asserted on rather than applied, for the reason in the module
    docstring - applying it would take `caplog`'s handler away."""
    json_config = logging_config(LogConfig(format="json"), "judge")
    assert json_config["formatters"]["cocco"] == {
        "()": "cocco.logs.JsonFormatter",
        "process": "judge",
    }
    assert json_config["root"]["level"] == "INFO"
    # uvicorn's own access line is replaced by `cocco.access`, never doubled.
    assert json_config["loggers"]["uvicorn.access"]["propagate"] is False

    text_config = logging_config(LogConfig(), "web", level="debug")
    assert "format" in text_config["formatters"]["cocco"]
    assert text_config["root"]["level"] == "DEBUG"

    # alembic's chatter is news to `cocco-migrate` only.
    assert text_config["loggers"]["alembic"]["level"] == "WARNING"
    migrate = logging_config(LogConfig(), "migrate")
    assert migrate["loggers"]["alembic"]["level"] == "INFO"
