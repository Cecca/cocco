"""The access log, and the request id that ties the app's lines to nginx's.

Two halves. The first runs `AccessLogMiddleware` around a toy Starlette app,
which is all the proxy and request-id rules need and costs no database. The
second runs the real app, because "the route is the template" and "the user is
on the line" are claims about how cocco's own routes and `get_user` meet the
middleware.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cocco.logs import (
    AccessLogMiddleware,
    ContextFilter,
    current_context,
    record_fields,
)

TRUSTED = ("127.0.0.1",)


@pytest.fixture(autouse=True)
def context_on_caplog(caplog):
    """The request id is bound context, which the real handler's filter puts
    on each record; `caplog`'s handler needs the same filter to see it."""
    caplog.handler.addFilter(ContextFilter())


def access_events(caplog) -> list[dict]:
    return [
        {"level": r.levelname, "msg": r.getMessage(), **record_fields(r), "_r": r}
        for r in caplog.records
        if r.name == "cocco.access"
    ]


@pytest.fixture
def toy():
    """A two-route app behind the middleware, reached from a chosen peer."""
    app = FastAPI()
    seen = {}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        # What the app's own log lines would be filed under.
        seen["context"] = dict(current_context())
        return {"ok": item_id}

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    def client(peer: str = "127.0.0.1", proxy_headers: bool = True) -> TestClient:
        wrapped = AccessLogMiddleware(
            app, proxy_headers=proxy_headers, trusted_proxies=TRUSTED
        )
        return TestClient(wrapped, client=(peer, 50000), raise_server_exceptions=False)

    return client, seen


########################################################################
# the toy app: proxies and request ids


def test_one_event_per_request_with_the_route_template(toy, caplog):
    client, seen = toy
    with caplog.at_level(logging.DEBUG, logger="cocco.access"):
        response = client(proxy_headers=False).get(
            "/items/12?code=s3cr3t&state=xyz",
            headers={"User-Agent": "x" * 1000, "HX-Request": "true"},
        )
    assert response.status_code == 200
    [event] = access_events(caplog)
    assert event["level"] == "INFO"
    assert event["method"] == "GET"
    assert event["status"] == 200
    assert event["route"] == "/items/{item_id}"
    assert event["path"] == "/items/12"
    assert event["client_ip"] == "127.0.0.1"
    assert event["hx"] is True
    assert len(event["user_agent"]) == 200
    assert event["duration_ms"] >= 0
    # The OAuth callback's query string carries `code` and `state`.
    assert "s3cr3t" not in str({k: v for k, v in event.items() if k != "_r"})
    # The id is echoed and is what the app's own lines were bound to.
    assert response.headers["X-Request-ID"] == event["request_id"]
    assert seen["context"]["request_id"] == event["request_id"]


def test_an_unmatched_path_has_no_route(toy, caplog):
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        assert client().get("/nowhere").status_code == 404
    [event] = access_events(caplog)
    assert event["route"] is None
    assert event["status"] == 404


def test_a_trusted_proxy_names_the_client_and_the_request_id(toy, caplog):
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        response = client().get(
            "/items/1",
            headers={"X-Forwarded-For": "203.0.113.9", "X-Request-ID": "abc123def"},
        )
    [event] = access_events(caplog)
    assert event["client_ip"] == "203.0.113.9"
    assert event["request_id"] == "abc123def"
    assert response.headers["X-Request-ID"] == "abc123def"


def test_a_forged_forwarded_for_only_adds_entries_to_the_left(toy, caplog):
    """nginx's `$proxy_add_x_forwarded_for` appends the peer it saw, so a
    client-forged header ends up on the left - and the right-most untrusted
    entry is the one believed."""
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        client().get(
            "/items/1", headers={"X-Forwarded-For": "10.6.6.6, 203.0.113.9"}
        )
    [event] = access_events(caplog)
    assert event["client_ip"] == "203.0.113.9"


def test_an_untrusted_peer_cannot_pick_its_address_or_its_request_id(toy, caplog):
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        response = client(peer="198.51.100.4").get(
            "/items/1",
            headers={"X-Forwarded-For": "203.0.113.9", "X-Request-ID": "chosen"},
        )
    [event] = access_events(caplog)
    assert event["client_ip"] == "198.51.100.4"
    assert event["request_id"] != "chosen"
    assert response.headers["X-Request-ID"] == event["request_id"]


def test_without_proxy_headers_even_loopback_is_not_believed(toy, caplog):
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        client(proxy_headers=False).get(
            "/items/1",
            headers={"X-Forwarded-For": "203.0.113.9", "X-Request-ID": "chosen"},
        )
    [event] = access_events(caplog)
    assert event["client_ip"] == "127.0.0.1"
    assert event["request_id"] != "chosen"


@pytest.mark.parametrize("offered", ["a" * 65, "has space", "new\nline", ""])
def test_a_request_id_that_does_not_look_like_one_is_replaced(toy, caplog, offered):
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        client().get("/items/1", headers={"X-Request-ID": offered})
    [event] = access_events(caplog)
    assert event["request_id"] != offered
    assert len(event["request_id"]) == 16


def test_an_exception_is_logged_as_a_500_with_its_traceback(toy, caplog):
    client, _ = toy
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        assert client().get("/boom").status_code == 500
    [event] = access_events(caplog)
    assert event["level"] == "ERROR"
    assert event["status"] == 500
    assert event["route"] == "/boom"
    assert event["_r"].exc_info[0] is RuntimeError


########################################################################
# the real app


def test_the_signed_in_user_is_on_the_line(make_client, caplog):
    client = make_client(as_="bob@unipd.it")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        assert client.get("/").status_code == 200
    [event] = access_events(caplog)
    assert event["route"] == "/"
    assert isinstance(event["user_id"], int)


def test_an_anonymous_request_has_no_user(make_client, caplog):
    client = make_client()
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        client.get("/login")
    [event] = access_events(caplog)
    assert event["route"] == "/login"
    assert event["user_id"] is None


def test_a_successful_fragment_poll_is_debug_and_a_failed_one_is_not(
    make_client, caplog
):
    client = make_client(as_="bob@unipd.it")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="cocco.access"):
        assert client.get("/f/courses").status_code == 200
        # A challenge that does not exist: the poll fails, and says so.
        assert client.get("/f/leaderboard/999").status_code >= 400
    ok, failed = access_events(caplog)
    assert ok["route"] == "/f/courses" and ok["level"] == "DEBUG"
    assert failed["route"] == "/f/leaderboard/{challenge_id}"
    assert failed["level"] == "INFO"


def test_a_static_file_is_filed_under_its_mount(make_client, caplog):
    client = make_client()
    with caplog.at_level(logging.INFO, logger="cocco.access"):
        client.get("/static/favicon.ico")
    [event] = access_events(caplog)
    assert event["route"] == "/static/{path}"
