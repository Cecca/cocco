---
status: done
---

# Structured logging, and the client's IP behind nginx

## Goal

Two things, one piece of work:

1. **Log lines a machine can query.** Every process — `cocco-web`, `cocco-judge`, `cocco-migrate` —
   writes one JSON object per line when a deployment asks for it, so "slowest route", "every 403 for
   this user" or "everything that happened to submission 812" is a `jq` or DuckDB query rather than
   a grep over prose.
2. **One access event per request, carrying the real client IP** even when the app sits behind
   nginx, with a request id that ties nginx's log line to the app's.

## Where things stand

- Every module already logs through `logging.getLogger(__name__)` with `%`-style messages. Those
  call sites do not need to change; only the *output* does.
- Each process configures logging its own way: `cocco-judge` calls `logging.basicConfig`
  (`cocco/judge.py:1131`), `cocco-web` only hands `log_level` to uvicorn, so the app's lines and
  uvicorn's come out in uvicorn's format. `cocco/server.py` writes its two refusals with `print`.
- The only per-request record is uvicorn's access log: a free-text line with no user, no duration
  and no route.
- `cocco-web --proxy-headers` already exists, and with it uvicorn's `ProxyHeadersMiddleware`
  rewrites `request.client.host` to the real client. But it only trusts connections from
  `forwarded_allow_ips`, which defaults to `127.0.0.1` and can only be changed through uvicorn's
  `FORWARDED_ALLOW_IPS` environment variable — a setting in the environment, which is the one place
  `config.py`'s rule says settings do not go.

## Design decisions

- **Stdlib `logging` plus a small JSON formatter, not structlog.** The formatter is ~30 lines;
  request-scoped fields (`request_id`, `user_id`, `submission_id`) come from `contextvars` through
  a `logging.Filter`, which is the main thing structlog would have bought. Same reasoning as
  `hashlib.scrypt` over a password library: no dependency when the stdlib is enough.
- **The module is `cocco/logs.py`**, never `cocco/logging.py`, which would shadow the stdlib.
- **Text is the default, JSON is opt-in.** `just dev` stays readable; a deployment sets
  `[log] format = "json"`.
- **Logging config reaches uvicorn as `log_config=`**, a `dictConfig` dict, never as handlers set up
  in the parent. Under `--reload` and `--workers` the app runs in child processes, which do not
  inherit the parent's handlers but do apply `log_config`. The same dict takes over
  `uvicorn.error`, so there is one format in the stream, not two.
- **uvicorn's access log is switched off** (`access_log=False`) and replaced by our own middleware,
  which knows the route template, the user and the duration.
- **Query strings are never logged.** The OAuth callback carries `code` and `state` in its query
  string, and those do not belong in a log file.
- **Output is stdout**, collected by systemd/journald or the container runtime. If a file is
  wanted, it is `WatchedFileHandler` + logrotate — never `RotatingFileHandler`, which under
  `--workers N` has several processes rotating the same file and overwriting each other.
- **Trusting a proxy is a setting, so it moves into the file**: `[site] trusted_proxies`, beside
  `[site] base_url`, which already depends on it.
- **Logs with IP addresses are kept for 6 months, then deleted.** Full addresses, not truncated:
  the purpose is security and incident investigation, which needs the address itself. Retention is
  enforced by the log collector (journald or logrotate), never by cocco — the app writes to stdout
  and does not know how old its output is. See "Retention" below.

## Steps

### [x] 1. Configuration: `[log]` and `[site] trusted_proxies`

- `cocco/config.py`: a `LogConfig` dataclass with `format: Literal["text", "json"] = "text"` and
  `level: str = "INFO"`, parsed from a `[log]` table, validated (an unknown format or level is a
  refusal at load, naming the accepted values).
- `[site] trusted_proxies: list[str]`, default `["127.0.0.1", "::1"]`. Addresses and CIDR networks,
  which is what uvicorn's `_TrustedHosts` already accepts. The deployment has nginx on the same
  host over loopback TCP, so the default is already right for it; the setting exists for the
  deployments where it is not.
- `--proxy-headers` moves to `[site] proxy_headers: bool = false` (false, as the flag is today, so
  a checkout with no proxy in front trusts no forwarded header). The flag is kept for one release
  and warned about in `_warn_about_removed_settings`'s pattern, naming the new spelling.
- `cocco.example.toml`: document both tables, including what the logs contain (see step 8).
- `cocco.dev.toml`: leave `[log]` absent, so the defaults apply.
- Tests in `test_config.py` for parsing, defaults and refusals.

### [x] 2. `cocco/logs.py`: the formatter, the context, the setup

- `JsonFormatter(logging.Formatter)`: emits `ts` (ISO 8601, UTC), `level`, `logger`, `msg`
  (the `%`-formatted message), `process` (`web` / `judge` / `migrate`), every field from the
  context, every key passed through `extra=`, and `exc` (the formatted traceback) when there is one.
  Non-JSON-serialisable values go through `str`, never crash the log call.
- Context: one `ContextVar[dict]` with a `bind(**fields)` context manager and a
  `ContextFilter` that copies the current context onto each record.
- `logging_config(log: LogConfig, process: str) -> dict`: the `dictConfig` dict for both formats,
  covering the root logger, `cocco.*`, `uvicorn.error` and silencing `uvicorn.access`.
- `configure_logging(log, process)`: `dictConfig(logging_config(...))`, for the processes that are
  not uvicorn.
- Unit tests (`test_logs.py`): one record in → one parseable JSON line out; context fields appear
  and disappear with `bind`; `extra=` fields appear; exceptions are serialised; a non-serialisable
  value does not raise.

### [x] 3. Wire it into the three processes

- `cocco/server.py`: read `CONFIG.log` (the file is already loaded by `_check_schema`), pass
  `log_config=logging_config(..., "web")`, `access_log=False`, and
  `forwarded_allow_ips=trusted_proxies` to `uvicorn.run`. Keep the lazy-import discipline:
  `cocco.logs` and `cocco.config`, never `cocco.main`. `--log-level` either goes away in favour of
  `[log] level` or overrides it — recommended: keep it as an override, it is the one setting you
  want to flip for a single run.
- The two `print(e, file=sys.stderr)` refusals become `logger.error`, after logging is configured.
- `cocco/judge.py:main`: replace `basicConfig` with `configure_logging(config.log, "judge")`;
  `--log-level` the same as for `cocco-web`.
- `cocco/migrate.py`: `configure_logging(config.log, "migrate")`.
- Test: the dict passed to uvicorn under `--reload` / `--workers` is the one built from the file
  (assert on the kwargs, as the existing server tests do).

### [x] 4. The access-log middleware

- A pure ASGI middleware in `cocco/main.py` (or `cocco/logs.py` if it needs nothing from the app),
  registered **outermost**, so its duration includes every other middleware.
- Per request: generate or adopt a `request_id` (see step 5), `bind` it for the duration of the
  request so every app log line carries it, and on completion emit one `cocco.access` event with:
  - `method`, `status`, `duration_ms`, `client_ip` (`request.client.host`, already rewritten by
    uvicorn when the proxy is trusted)
  - `route`: the matched route's template (`/courses/{course_id}/{challenge_id}`), `null` for a 404
  - `user_id` from `request.state.user` when `get_user` set it
  - `hx`: whether `HX-Request` was present
  - `user_agent`, truncated
  - **not** the query string
- Polled fragments: a successful (`2xx`/`304`) request to a `/f/...` route is logged at DEBUG,
  everything else at INFO — so a failing poll still shows up at the default level.
- An exception escaping the app is logged with status 500 and the traceback, and re-raised.
- Response header `X-Request-ID` echoed back, so a user reporting a problem can quote it.
- Tests: one event per request with the expected fields; route is the template, not the path;
  no query string; `user_id` present when signed in and absent otherwise; fragment polls at DEBUG.

### [x] 5. Request ids, and correlation with nginx

- Adopt an incoming `X-Request-ID` **only from a trusted proxy** (same `trusted_proxies` list),
  and only if it looks like an id (bounded length, `[A-Za-z0-9-]`); otherwise generate one
  (`secrets.token_hex(8)`). A client must not be able to pick the id another request is filed under.
- Document the nginx side in `cocco.example.toml` (step 8).

### [x] 6. Context in the judge

- `run_submission` / the loop body `bind(submission_id=..., challenge_id=..., language=...)` for
  the duration of grading one submission, so every line emitted underneath — compile, each test
  case, the evaluator — carries them without being edited.
- Test: a log line emitted inside a graded run carries `submission_id`.

### [x] 7. Promote the fields worth querying

Not a rewrite: the `%`-style messages stay. Where a value is something one would filter on, add it
through `extra=` too. Candidates:
- `main.py` sign-in refusals and the dummy sign-in: `email`, `provider`, `reason`.
- `judge.py`: outcome per test case, `elapsed_s`, `peak_memory_bytes`, the final score.
- `config.py` / `auth.py` warnings: the setting name.

### [x] 8. Documentation

- `cocco.example.toml`:
  - the `[log]` table, and that JSON lines go to stdout;
  - `trusted_proxies`, and what silently goes wrong without it (every request logged with nginx's
    address — nginx in another container, on another host, or reaching the app over `::1`);
  - the nginx snippet:
    ```nginx
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID      $request_id;
    ```
    and why `$proxy_add_x_forwarded_for` and never `$http_x_forwarded_for`: uvicorn takes the
    right-most untrusted entry, so a client-forged header only adds entries to the left;
  - **what the logs contain, and for how long**: IP addresses, user ids and email addresses are
    personal data (GDPR), kept in full for **6 months** and then deleted, with the journald and
    logrotate settings from "Retention" below.
- `CLAUDE.md`: a short "Logging" section — the module, the uvicorn `log_config` trap, the
  no-query-string rule, the trusted-proxies setting.
- A few example queries (`jq`, DuckDB `read_json_auto`) in the example config or the README.

### [x] Retention

Six months, as the purpose-bound period for security logs: it matches the Italian Garante's minimum
for system-administrator access logs (2008) and the low end of CNIL's 6–12 month recommendation.
It is a GDPR storage-limitation decision (Art. 5(1)(e)), so it also belongs in the university's
record of processing (Art. 30) and in the privacy notice students see (Art. 13) — to be confirmed
with the University of Padova's DPO.

What `cocco.example.toml` documents:

- **journald**: `MaxRetentionSec=6month` in `journald.conf` (or a drop-in). It applies to the whole
  journal on the host, not just cocco's unit — say so, so nobody is surprised.
- **File + logrotate**: `daily`, `rotate 183`, `maxage 183`, `compress`, with `WatchedFileHandler`
  on cocco's side (see the design decision on handlers).
- **nginx's own access log carries the same IPs** and needs the same 6 months; leaving it on the
  distribution's default makes the app-side retention meaningless.
- **Backups** of the log directory fall under the same period.
- Aggregates derived from the logs with no IP and no user id (requests per route, error rates,
  timings) are not personal data and may be kept as long as useful.

### [x] 9. Verify end to end

- Run `cocco-web` with `[log] format = "json"` behind a local nginx on loopback; check the access
  event carries the client's address, not `127.0.0.1`, and that a forged `X-Forwarded-For` from
  the client does not change it.
- Check the same under `--workers 2`, to prove the config reaches the children.
- Run the judge on a submission and query its lines by `submission_id`.

## Decided

- **Library**: stdlib `logging` with our own JSON formatter, not structlog.
- **Fragment polls**: successful ones logged at DEBUG.
- **Proxy headers**: `--proxy-headers` moves to `[site] proxy_headers`, the flag deprecated for
  one release.
- **Deployment topology**: nginx on the same host, over loopback TCP — the default
  `trusted_proxies` covers it, and no Unix-socket support is needed.
- **IP retention**: full addresses, kept for 6 months, then deleted (see "Retention").

## Summary

Implemented as planned, on 2026-10-17. All 652 tests pass (`uv run pytest`, integration included),
and the end-to-end check in step 9 was run for real.

**What landed**

- `cocco/config.py`: `LogConfig` (`[log] format`/`level`), `[site] proxy_headers` and
  `[site] trusted_proxies` on `AppConfig`, and a `ConfigError` for the settings with no safe
  reading — an unknown format or level, and a proxy entry that is neither an address nor a CIDR
  network (uvicorn would keep it as a literal that matches nothing). `load_log_config` reads the
  `[log]` table alone, so each process configures logging *before* `load_config` and its warnings
  come out in the requested format. Config/auth warnings carry `extra={"setting": ...}`.
- `cocco/logs.py`: `JsonFormatter`, `bind` + `ContextFilter`, `logging_config` / `configure_logging`,
  and `AccessLogMiddleware`. `cocco-web` passes the dict to uvicorn as `log_config=` with
  `access_log=False` and `log_level=None`; the judge and `cocco-migrate` call `configure_logging`.
  `--log-level` is now an override of `[log] level` on all three commands. `server.py`'s two
  `print` refusals are `logger.error`.
- `cocco.access`: one event per request with method, path, route template, status, duration,
  client IP, user id, `hx` and a truncated user agent; never the query string. Successful `/f/...`
  polls at DEBUG; an escaping exception is ERROR with its traceback, then re-raised.
  `X-Request-ID` is echoed.
- The judge binds `submission_id`, `challenge_id` and `language` around each graded run
  (`challenge_id` is a new, defaulted field on `SubmissionRunData`); per-test lines carry
  `testcase_id`, `outcome`, `elapsed_s`, `peak_memory_bytes`, and the final line `score`/`n_tests`.
  Sign-in refusals carry `email`, `provider` and `reason` (`_complete_sign_in` takes `provider=`).
- Tests: `test_logs.py`, `test_access_log.py`, `test_server.py`, new cases in `test_config.py` and
  `test_judge.py`.
- Docs: `cocco.example.toml` (`[site]` proxy settings with the nginx snippet, `[log]`, example `jq`
  and DuckDB queries, the six-month retention and how journald/logrotate enforce it), a "Logging"
  section in `CLAUDE.md`, and `migrations/env.py`'s note.

**Where it departs from the plan, and why**

- **The proxy is handled inside the app, not by uvicorn.** Step 5 wants `X-Request-ID` adopted only
  from a trusted proxy, but uvicorn's `ProxyHeadersMiddleware` runs *outside* the app and overwrites
  `scope["client"]`, so by the time any app middleware runs the proxy's address is gone.
  `AccessLogMiddleware` therefore wraps `ProxyHeadersMiddleware` inside itself when
  `[site] proxy_headers` is on, checks the raw peer first, and uvicorn is started with
  `proxy_headers=False`. Side benefit: it works under any ASGI server. `forwarded_allow_ips` is
  still passed to uvicorn (from the file), but only matters for the deprecated `--proxy-headers`
  flag, which keeps working for client IP and scheme but cannot adopt nginx's request id.
- **`cocco-migrate` logs to stderr**, not stdout: `upgrade --sql` writes DDL to stdout for
  redirection into a file.
- **`alembic` is held at WARNING in web and judge.** With root now configured at INFO, the schema
  check's "setup plugin …" chatter became visible in both servers; `cocco-migrate` keeps it.
- **Log lines also carry `path`** (without query string) beside `route`, so a 404 — where `route`
  is null — still says what was asked for.
- **Retention docs describe stdout → file + logrotate (`copytruncate`)** rather than a
  `WatchedFileHandler`: cocco has no file-handler setting, by the "output is stdout" decision.
- The example config also notes that **nginx's default `$request` logs the query string**, OAuth
  `code` included, and suggests `$request_method $uri`.

**Step 9, as run**: nginx on the LAN address (192.168.1.35:8088) proxying over loopback to
`cocco-web --workers 2` with `[log] format = "json"`. The access events carried `client_ip`
192.168.1.35, not 127.0.0.1; a client-forged `X-Forwarded-For: 6.6.6.6` and `X-Request-ID: forged`
changed neither; the `request_id` on cocco's lines matched `rid=$request_id` in nginx's log; both
worker processes wrote JSON. A submission graded by `cocco-judge` was queryable with
`jq 'select(.submission_id == 9)'`, every line carrying `challenge_id` and `language`.

**Still open**: the six-month retention needs confirming with the University of Padova's DPO and
recording in the Art. 30 register and the Art. 13 notice — that is not a code change.
