---
paths:
  - "cocco/logs.py"
  - "cocco/server.py"
  - "cocco/test_logs.py"
  - "cocco/test_access_log.py"
  - "cocco/test_server.py"
---

# Logging (`cocco/logs.py`)

**Call sites do not change; only the output does.** Every module logs through
`logging.getLogger(__name__)` with `%`-style messages, and `[log] format = "json"` turns each record
into one JSON object on stdout (`JsonFormatter`); `"text"`, the default, is what a developer reads. A
value worth filtering on is *also* passed through `extra=` — the judge's per-test `outcome`,
`elapsed_s` and the final `score`; the sign-in refusals' `email`, `provider` and `reason`; the
config warnings' `setting`. The module is `logs`, never `logging`, which would shadow the stdlib.
Output is stdout and nothing else (stderr for `cocco-migrate`, whose `--sql` owns stdout); retention
is the collector's job, six months, and `cocco.example.toml` says how and why.

**Request-scoped fields are `bind`, a `ContextVar`, copied onto each record by `ContextFilter`** —
on the *handler*, because a logger's filters do not run for records propagated from its children.
`AccessLogMiddleware` binds `request_id` for a request and `run_from_queue` binds `submission_id`,
`challenge_id` and `language` for a graded run, so every line underneath carries them without being
edited. A test reading those off `caplog` has to add a `ContextFilter` to `caplog.handler`.

**Logging reaches uvicorn as `log_config=`, a `dictConfig` dict, never as handlers set up in
`cocco-web`'s process.** Under `--reload` and `--workers` the app runs in a child that inherits none
of the parent's handlers but does apply `log_config` — and the same dict takes over `uvicorn.error`,
so there is one format in the stream. `log_level` is passed as None because it would override the
dict's levels, and `access_log=False` because `cocco.access` replaces uvicorn's line. Each process
configures logging from `load_log_config` — the `[log]` table alone — *before* `load_config`, so the
latter's warnings come out in the format the deployment asked for; a `[log]` table that cannot be
read is the one refusal that is a bare `print`, having no log to go to. `--log-level` on any of the
three commands overrides the file for one run.

**One `cocco.access` event per request**, from `AccessLogMiddleware`, a pure ASGI middleware
registered *last* in `main.py` so it is outermost (`add_middleware` puts each new one outside the
others) — pure ASGI rather than `BaseHTTPMiddleware`, which would run the app in another task and lose
the bound context. It carries the route **template** (FastAPI's `scope["route"]`, a mount's prefix +
`/{path}`, None for a 404), `status`, `duration_ms`, `client_ip`, `user_id` from the
`request.state.user` that `get_user` stashes, `hx` and a truncated `user_agent` — and **never the
query string**, because the OAuth callback's carries `code` and `state`. A 2xx/304 on a `/f/...`
route is DEBUG and everything else INFO, so a failing poll still shows; an exception escaping the app
is ERROR with its traceback, then re-raised. The id goes back as `X-Request-ID`.

**`[site] proxy_headers` is handled inside the app, not by uvicorn**, and the order is the point:
`AccessLogMiddleware` wraps uvicorn's `ProxyHeadersMiddleware` *inside* itself, because an incoming
`X-Request-ID` is adopted only from a peer in `[site] trusted_proxies` (and only if it matches
`[A-Za-z0-9-]{1,64}`) — otherwise a client could choose the id another request is filed under — and
the proxy's own address is only visible before `ProxyHeadersMiddleware` rewrites `scope["client"]`.
It also keeps the trusted list in the file rather than in uvicorn's `FORWARDED_ALLOW_IPS`, which is
the environment. `config.py` validates every entry as an address or a CIDR network, because uvicorn
keeps anything unparseable as a literal that matches nothing — and the symptom of that is every
request logged with nginx's address. `cocco-web --proxy-headers` survives one release, warned about:
it still makes uvicorn rewrite the client and scheme (from the same trusted list), but only the file's
spelling adopts nginx's request id.
