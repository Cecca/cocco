"""How every cocco process writes its log: one line per event, on stdout.

Stdlib `logging` and nothing on top of it. Every module already logs through
`logging.getLogger(__name__)` with `%`-style messages, and none of those call
sites changes - only the *output* does, which is this module's whole job:

- `JsonFormatter` turns a record into one JSON object per line, for a
  deployment that sets `[log] format = "json"` and wants to ask its log
  questions with `jq` or DuckDB instead of grep.
- `bind` and `ContextFilter` are the request-scoped half: a field bound for the
  duration of a request (`request_id`, `user_id`) or of a graded submission
  (`submission_id`) lands on every line emitted underneath it, without any of
  those lines being edited. That is the one thing structlog would have bought,
  and it is a `ContextVar` and a filter.
- `logging_config` is the `dictConfig` dict, and the reason it is a dict rather
  than handlers set up here: `cocco-web` runs the app under uvicorn, and under
  `--reload` or `--workers` the app lives in a child process that inherits none
  of the parent's handlers but does apply uvicorn's `log_config`. So the same
  dict is what `configure_logging` applies for the judge and `cocco-migrate`,
  and what `cocco/server.py` hands uvicorn.

Called `logs` and never `logging`, which would shadow the stdlib for every
module in the package that imports it.

Output is stdout and nothing else. Collecting it, rotating it and deleting it
after six months is the collector's business - journald, a container runtime,
or logrotate over a `WatchedFileHandler` - because the app does not know how
old its output is. Never `RotatingFileHandler`: under `--workers N` several
processes would rotate the same file and overwrite each other.
"""

import json
import logging
import logging.config
import re
import secrets
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from cocco.config import LogConfig

# What one request or one graded submission is about. A dict replaced on every
# `bind`, never mutated, so a task that copied the context (asyncio does, on
# `create_task`) cannot see a field bound after it started, nor leak one back.
_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("cocco_log_context", default={})

# The attribute `ContextFilter` puts the context under. Underscored, so the
# formatter can tell it from an `extra=` key, and so an `extra=` key spelled
# like a context field wins over the context rather than being clobbered by it.
_CONTEXT_ATTR = "_cocco_context"

# Every attribute a bare LogRecord has, plus the two `Formatter.format` adds -
# anything else on a record came in through `extra=`. Computed rather than
# listed, so a Python that grows one (3.12 added `taskName`) cannot turn it
# into a field on every line.
_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}

# Keys some library passes through `extra=` that are presentation, not data:
# uvicorn hands its own formatter a copy of the message with ANSI colours in.
_IGNORED_EXTRA = frozenset({"color_message"})


def current_context() -> dict[str, Any]:
    return _CONTEXT.get()


@contextmanager
def bind(**fields: Any) -> Generator[None]:
    """Attach `fields` to every log line emitted inside the block.

    Nested binds add to the outer one and are undone on the way out; a field
    bound to None is left out, so `bind(user_id=None)` is harmless.
    """
    token = _CONTEXT.set(
        {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    )
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class ContextFilter(logging.Filter):
    """Copy the bound context onto each record.

    On the *handler*, never on a logger: a logger's filters do not run for
    records propagated up from its children, and every cocco line reaches the
    root's handler by propagation.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _CONTEXT.get()
        if context:
            setattr(record, _CONTEXT_ATTR, context)
        return True


def record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The structured half of a record: the bound context, then whatever the
    call site passed through `extra=`, the latter winning a tie."""
    fields = dict(getattr(record, _CONTEXT_ATTR, {}))
    for key, value in record.__dict__.items():
        if key in _RECORD_ATTRS or key in _IGNORED_EXTRA or key.startswith("_"):
            continue
        fields[key] = value
    return fields


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    The fixed keys come first - `ts` (ISO 8601, UTC), `level`, `logger`, `msg`,
    `process` - then the context and the `extra=` fields, then `exc` when the
    record carries a traceback. A field may not overwrite a fixed key: a call
    site passing `extra={"level": ...}` would otherwise make the line lie about
    its own severity.

    Nothing a call site passes can make the log call raise: a value JSON cannot
    represent goes through `str`, because a log line that crashes the request
    it was describing is worse than one with a repr in it.
    """

    def __init__(self, process: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.process = process

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "process": self.process,
        }
        for key, value in record_fields(record).items():
            line.setdefault(key, value)
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            line["exc"] = record.exc_text
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, default=str, ensure_ascii=False)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def logging_config(
    log: LogConfig,
    process: str,
    level: str | None = None,
    stream: str = "ext://sys.stdout",
) -> dict:
    """The `dictConfig` dict for `process` ("web", "judge" or "migrate").

    `level` overrides `log.level` - it is what `--log-level` passes, the one
    setting worth flipping for a single run without editing the file. `stream`
    is stdout for the two servers; `cocco-migrate` passes stderr, because its
    `--sql` writes DDL to stdout for somebody to redirect into a file.

    One handler, on the root, and every logger that matters propagates to it:
    `cocco.*`, `uvicorn.error` (whose own handler uvicorn would otherwise
    install, giving two formats in one stream) and the libraries. `uvicorn.access`
    is silenced outright - `cocco.access` replaces it with a line that knows the
    route, the user and the duration - and so is propagation from it, so a
    stray access record cannot reach the root by the back door.
    `disable_existing_loggers` is False because every module's logger exists by
    the time this runs, and disabling them is how a judge goes silent.
    """
    level = (level or log.level).upper()
    if log.format == "json":
        # Named by string rather than by object: under `--workers` uvicorn
        # pickles its whole config into each child, this dict included.
        formatter = {"()": "cocco.logs.JsonFormatter", "process": process}
    else:
        formatter = {"format": TEXT_FORMAT}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": "cocco.logs.ContextFilter"}},
        "formatters": {"cocco": formatter},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": stream,
                "formatter": "cocco",
                "filters": ["context"],
            }
        },
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            "cocco": {"level": level},
            "uvicorn": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False},
            # The schema check runs alembic on every start, and its INFO lines
            # ("setup plugin ...", "Context impl ...") are news only to
            # `cocco-migrate`, which is there to say what alembic did.
            "alembic": {"level": level if process == "migrate" else "WARNING"},
        },
    }


def configure_logging(
    log: LogConfig,
    process: str,
    level: str | None = None,
    stream: str = "ext://sys.stdout",
) -> None:
    """Apply `logging_config`. For the judge and `cocco-migrate`, which uvicorn
    is not running - and for `cocco-web`'s own process before uvicorn starts,
    so its refusals are written in the same format as everything after."""
    logging.config.dictConfig(logging_config(log, process, level, stream))


########################################################################
# the access log

access_logger = logging.getLogger("cocco.access")

# What an id adopted from the proxy may look like. nginx's `$request_id` is 32
# hex digits; this admits that and a UUID, and nothing that could carry a
# newline, a quote or a megabyte into somebody's log.
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")

# A user agent is attacker-chosen text of any length, and a log line is not
# where it gets to be long.
MAX_USER_AGENT = 200


class AccessLogMiddleware:
    """One `cocco.access` event per HTTP request, and the request id every other
    line of that request is filed under.

    Pure ASGI rather than `BaseHTTPMiddleware`, which runs the app in another
    task - and a context bound in one task is not seen by the other, so the
    request id would not reach the app's own log lines. Registered *outermost*
    in `cocco.main`, so `duration_ms` includes every other middleware.

    It also owns the proxy, when there is one: `proxy_headers` wraps the app in
    uvicorn's `ProxyHeadersMiddleware` *inside* this one, rather than leaving
    that to uvicorn, which would run it outside. That order is the point. An
    incoming `X-Request-ID` is adopted only from a trusted proxy - otherwise a
    client could pick the id somebody else's request is filed under - and the
    only moment the proxy's own address is visible is before
    `ProxyHeadersMiddleware` has replaced it with the client's. It also means
    the trusted list is the file's `[site] trusted_proxies`, whatever ASGI
    server runs the app, and never uvicorn's `FORWARDED_ALLOW_IPS`.

    What the event carries is in `_log`. What it never carries is the query
    string: the OAuth callback's has the `code` and the `state` in it.
    """

    def __init__(
        self,
        app,
        proxy_headers: bool = False,
        trusted_proxies: Sequence[str] = (),
    ) -> None:
        self.app = app
        self.trusted = None
        if proxy_headers:
            proxy = ProxyHeadersMiddleware(app, trusted_hosts=list(trusted_proxies))
            self.app = proxy
            self.trusted = proxy.trusted_hosts

    def _request_id(self, scope, headers: dict[bytes, bytes]) -> str:
        peer = scope.get("client")
        if self.trusted is not None and peer and peer[0] in self.trusted:
            offered = headers.get(b"x-request-id", b"").decode("latin-1")
            if _REQUEST_ID.fullmatch(offered):
                return offered
        return secrets.token_hex(8)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        headers = dict(scope["headers"])
        request_id = self._request_id(scope, headers)
        # Created here, so the dict `get_user` stashes the viewer in is one this
        # middleware is still holding when the response has gone.
        scope.setdefault("state", {})
        root_path = scope.get("root_path", "")
        status: int | None = None

        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                # Echoed, so somebody reporting a problem can quote it.
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"x-request-id", request_id.encode()),
                    ],
                }
            await send(message)

        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                # Logged and re-raised: Starlette's outermost handler is what
                # turns it into the 500 page, and uvicorn what reports it.
                self._log(scope, headers, root_path, 500, started, failed=True)
                raise
            self._log(scope, headers, root_path, status, started)

    def _log(
        self,
        scope,
        headers: dict[bytes, bytes],
        root_path: str,
        status: int | None,
        started: float,
        failed: bool = False,
    ) -> None:
        route = _route_template(scope, root_path)
        user = scope["state"].get("user")
        client = scope.get("client")
        fields = {
            "method": scope["method"],
            "path": scope["path"],
            "route": route,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            # Already the real client when the proxy is trusted: the
            # `ProxyHeadersMiddleware` this wraps rewrote the scope in place.
            "client_ip": client[0] if client else None,
            "user_id": getattr(user, "id", None),
            "hx": b"hx-request" in headers,
            "user_agent": headers.get(b"user-agent", b"")
            .decode("latin-1")[:MAX_USER_AGENT]
            or None,
        }
        if failed:
            level = logging.ERROR
        elif (
            route is not None
            and route.startswith("/f/")
            and status is not None
            and (200 <= status < 300 or status == 304)
        ):
            # A polled fragment that worked is the bulk of all traffic and
            # tells nobody anything; one that failed still shows at INFO.
            level = logging.DEBUG
        else:
            level = logging.INFO
        access_logger.log(
            level,
            "%s %s %s %.1fms",
            fields["method"],
            fields["path"],
            status,
            fields["duration_ms"],
            extra=fields,
            exc_info=failed,
        )


def _route_template(scope, root_path: str) -> str | None:
    """The matched route as it was declared - `/courses/{course_id}`, not
    `/courses/12` - which is what "slowest route" has to group by. None when
    nothing matched, which is a 404.

    FastAPI puts its `APIRoute` on the scope. A mount does not, but it moves
    `root_path` along by the prefix it matched, which is all a template for
    one is: `/static/{path}`.
    """
    route = scope.get("route")
    if route is not None:
        return route.path
    mounted = scope.get("root_path", "")
    if scope.get("endpoint") is not None and mounted != root_path:
        return mounted[len(root_path) :] + "/{path}"
    return None
