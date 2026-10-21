from cocco.logs import AccessLogMiddleware
from fastapi import (
    FastAPI,
)
from fastapi.responses import (
    HTMLResponse,
)
from starlette.middleware.sessions import SessionMiddleware
import logging


from cocco.web import (
    admin,
    api,
    challenge_editor,
    challenges,
    core,
    courses,
    exercise_sessions,
    exercise_sets,
    exercise_student,
    home,
    people,
    profile,
    signin,
    testcases,
)
from cocco.web.access import (
    Blocked,
    Forbidden,
    NotAuthenticated,
    handle_blocked,
    handle_forbidden,
    handle_not_authenticated,
)
from cocco.web.seed import (
    prepare_database,
)
from cocco.web.templating import (
    STATIC_DIR,
    VersionedStaticFiles,
)


logger = logging.getLogger(__name__)

# Root of the app
app = FastAPI(default_response_class=HTMLResponse)
app.add_exception_handler(NotAuthenticated, handle_not_authenticated)
app.add_exception_handler(Forbidden, handle_forbidden)
app.add_exception_handler(Blocked, handle_blocked)

# The session cookie carries nothing but a user id; everything shown about a
# user is re-read from the database on every request.
app.add_middleware(
    SessionMiddleware,
    secret_key=core.CONFIG.session.secret_key,
    session_cookie=core.CONFIG.session.cookie_name,
    max_age=core.CONFIG.session.max_age,
    https_only=core.CONFIG.session.https_only,
    # Not "strict": the return from a provider is a top-level cross-site
    # navigation, and under "strict" the cookie carrying the OAuth `state`
    # would not be sent to the callback, so every sign-in would fail as a state
    # mismatch.
    same_site="lax",
)

# Added last, so it runs first: `add_middleware` puts each new one outside the
# ones before it, and the access log's duration has to include all of them. It
# also owns `[site] proxy_headers` - see `AccessLogMiddleware` for why the
# proxy is handled in here rather than by uvicorn.
app.add_middleware(
    AccessLogMiddleware,
    proxy_headers=core.CONFIG.proxy_headers,
    trusted_proxies=core.CONFIG.trusted_proxies,
)

app.mount("/static", VersionedStaticFiles(directory=STATIC_DIR), name="static")

app.include_router(signin.router)
app.include_router(home.router)
app.include_router(admin.router)

# Route order is load-bearing: `/courses/{course_id}/{challenge_id}` matches any
# single segment, so every other two-segment course page (the editor, people,
# exercises, sessions) must be included or declared above `get_challenge`.
app.include_router(courses.router)
app.include_router(people.router)
app.include_router(exercise_sets.router)
app.include_router(exercise_sessions.router)
app.include_router(exercise_student.router)
app.include_router(challenges.router)
app.include_router(challenge_editor.router)
app.include_router(testcases.router)
app.include_router(profile.router)
app.include_router(api.router)


@app.on_event("startup")
async def on_startup():
    # An ordinary boot only *checks* the schema; `cocco-migrate` is what builds
    # it. `cocco/server.py` asks the same question before uvicorn is even
    # started - see the comment there for why one of the two is not enough -
    # and this is the copy that covers every other way `cocco.main:app` is
    # served, and that makes each worker verify for itself.
    #
    # Resetting drops the schema and reseeds the dummy data, so it must not
    # happen on an ordinary boot: `cocco-web --reload` reloads on every file
    # change and each one would wipe every submission made while testing.
    await prepare_database(reset=core.CONFIG.dev.reset_db)
