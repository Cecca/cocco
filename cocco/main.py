from pathlib import Path
from typing import Annotated
import markdown as md
from markupsafe import Markup
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound
from sqlalchemy import insert, select
from cocco.avatar import generate_identicon
from cocco.evaluators import BUNDLED, DEFAULT_EVALUATOR, bundled_path
from cocco.judge import extension_to_language, load_languages
from cocco.storage import Storage, safe_filename
from cocco.model import (
    Challenge,
    Course,
    LeaderboardRanking,
    OutcomeEnum,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
    UserRole,
    metadata,
)
from cocco import schema
from cocco.auth import (
    get_or_create_user,
    google_redirect_uri,
    is_allowed_email,
    load_config,
    normalize_email,
    safe_next_path,
)
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from starlette.middleware.sessions import SessionMiddleware
import io
import logging
import os

from cocco.repository import (
    challenge_in_course,
    count_running_submissions,
    count_testcase_outcomes,
    course_exists,
    delete_testcase,
    insert_challenge,
    insert_course,
    insert_enrollment,
    insert_self_enrollment,
    insert_submission,
    insert_testcase,
    insert_user,
    query_challenge,
    query_challenge_leaderboard,
    query_challenge_ranking,
    query_challenge_standings,
    query_challenge_testcases,
    query_course,
    query_course_challenges,
    query_course_info,
    query_course_role,
    query_courses,
    query_evaluator_path,
    query_ordered_challenge_ids,
    query_sample_testcase,
    query_submission,
    query_submission_outcomes,
    query_testcase,
    query_user,
    query_user_role,
    query_user_submissions,
    regrade_challenge,
    reorder_challenges,
    swap_testcase_order,
    update_challenge,
    update_course,
    update_testcase,
    user_is_admin,
)


logger = logging.getLogger(__name__)

# The Google client, the allowed domains, the session secret and the public
# base URL. A missing file is fine: the app boots and the login page says that
# Google sign-in is unconfigured.
CONFIG = load_config(Path(os.environ.get("COCCO_CONFIG", "cocco.toml")))

# The development cast, up here because DUMMY_USER defaults to the admin.
# Matteo is the administrator, Alice a plain teacher - the two are course staff
# alike, and the difference only shows on "New course", which `require_admin`
# gates. Bob and Charlie are students.
DUMMY_ADMIN = "matteo.ceccarello@unipd.it"
DUMMY_TEACHERS = ["alice@unipd.it"]
DUMMY_STAFF = [DUMMY_ADMIN, *DUMMY_TEACHERS]
DUMMY_STUDENTS = ["bob@unipd.it", "charlie@unipd.it"]

# Development shortcut: no Google, no click, no credentials. `get_user` is the
# only place that branches on it, so every route is signed in transparently.
DUMMY_AUTH = os.environ.get("COCCO_DUMMY_AUTH") == "1"
# The admin rather than any of the staff: a fresh checkout should be able to
# create a course, and `COCCO_DUMMY_USER=alice@unipd.it` is then the way to see
# what a teacher sees.
DUMMY_USER = os.environ.get("COCCO_DUMMY_USER") or DUMMY_ADMIN

# Root of the app
app = FastAPI(default_response_class=HTMLResponse)

# The session cookie carries nothing but a user id; everything shown about a
# user is re-read from the database on every request.
app.add_middleware(
    SessionMiddleware,
    secret_key=CONFIG.session.secret_key,
    session_cookie=CONFIG.session.cookie_name,
    max_age=CONFIG.session.max_age,
    https_only=CONFIG.session.https_only,
    # Not "strict": the return from Google is a top-level cross-site
    # navigation, and under "strict" the cookie carrying the OAuth `state`
    # would not be sent to the callback, so every sign-in would fail as a state
    # mismatch.
    same_site="lax",
)

# Registered at import, but nothing is fetched here: Authlib pulls Google's
# metadata document lazily, on the first `authorize_redirect`. That is what
# lets a checkout with no credentials boot and merely say so on the login page.
oauth = OAuth()
if CONFIG.auth.google is not None:
    oauth.register(
        name="google",
        client_id=CONFIG.auth.google.client_id,
        client_secret=CONFIG.auth.google.client_secret,
        server_metadata_url=CONFIG.auth.google.server_metadata_url,
        client_kwargs={
            # `profile` is deliberately not asked for: the nickname is derived
            # from the address, so the name and the picture would be data we
            # hold without using. Both of these are non-sensitive scopes, so
            # Google asks for no verification review.
            "scope": "openid email",
            "code_challenge_method": "S256",
        },
    )


def google_client():
    """The registered client, or None when there are no credentials.

    A function rather than a module constant so a test can replace the whole of
    Google with two methods.
    """
    return oauth.create_client("google")

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# The database
url = f"postgresql+psycopg://{os.environ['PGUSER']}@/{os.environ['PGDATABASE']}?host={os.environ['PGHOST']}"
engine = create_async_engine(url)

# The templates directory
templates = Jinja2Templates(directory="templates")

# Test case inputs and expected outputs live on the filesystem, not in the
# database (see DESIGN.md). The judge must be pointed at the same root, either
# through this variable or through its own --storage flag.
storage = Storage(Path(os.environ.get("COCCO_STORAGE", "/tmp/cocco-storage")))

# Uploads are mapped to a language by their extension, so the challenge page
# needs no language picker. `languages.toml` is the same file the judge loads,
# and so is the mapping - an evaluator script is recognised by exactly the rule
# a submitted solution is.
LANGUAGES = load_languages(Path("languages.toml"))
EXTENSION_TO_LANGUAGE = extension_to_language(LANGUAGES)

MAX_UPLOAD_BYTES = 1 << 20

# An evaluator is a source file, not test data, so it lives under the same
# ceiling as a submitted solution rather than the test case one.
MAX_EVALUATOR_BYTES = MAX_UPLOAD_BYTES

# Test case inputs are routinely far larger than a solution file, so they get
# their own, much higher ceiling - and are streamed to disk, never buffered.
MAX_TESTCASE_BYTES = 32 << 20

# Course staff. `role` lives on `enrollment`, so these are per-course, not
# site-wide. Everyone else - i.e. students and the unenrolled - is read-only.
EDITOR_ROLES = {UserRole.teacher, UserRole.admin, UserRole.collaborator}
# The same set as it comes back from `row_to_json`, i.e. plain strings
EDITOR_ROLE_NAMES = {role.value for role in EDITOR_ROLES}


def render_markdown(text: str | None) -> Markup:
    html = md.markdown(text or "", extensions=["fenced_code", "tables"])
    return Markup(html)

# Line numbers in their own table column so that selecting the code does not
# select them too (see `.linenos` in assets/styles.css). `cocco-code` is the
# class the token styles are scoped under.
_SOURCE_FORMATTER = HtmlFormatter(cssclass="cocco-code", linenos="table", wrapcode=True)


def render_source(source: str, language: str) -> Markup:
    """A submission's source, highlighted. `language` is the name from
    `languages.toml`, which for every language we support happens to be a
    Pygments alias too; anything else falls back to unhighlighted text."""
    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        lexer = TextLexer()
    return Markup(pygments_highlight(source, lexer, _SOURCE_FORMATTER))


def render_datetime(value):
    from babel.dates import format_datetime
    from datetime import datetime
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return format_datetime(value, locale="en")

def render_score(value) -> str:
    """Per-test scores are in [0, 1]; the UI shows their mean out of 100."""
    if value is None:
        return "—"
    return f"{round(float(value) * 100)}"

def render_duration(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value < 1:
        return f"{value * 1000:.0f} ms"
    return f"{value:.2f} s"

templates.env.filters["markdown"] = render_markdown
templates.env.filters["highlight"] = render_source
templates.env.filters["datetime"] = render_datetime
templates.env.filters["score"] = render_score
templates.env.filters["duration"] = render_duration

# A global rather than a context key: `_course-card.jinja` is included from two
# places and only needs to know whether a `role` string means course staff.
templates.env.globals["EDITOR_ROLE_NAMES"] = EDITOR_ROLE_NAMES


async def create_db_and_tables(dummy=False):
    async with engine.begin() as conn:
        if dummy:
            await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
        if dummy:
            await _populate_dummy_data(conn)


async def _seed_user(conn: AsyncConnection, email: str, nickname: str) -> int:
    image, image_mime = generate_identicon(nickname)
    return await insert_user(conn, email, nickname, image, image_mime)


async def _populate_dummy_data(conn: AsyncConnection):
    import textwrap

    user_ids = {
        email: await _seed_user(conn, email, email.split("@", 1)[0])
        for email in DUMMY_STAFF + DUMMY_STUDENTS
    }
    # The student whose attempts the seeded submission history belongs to.
    student_id = user_ids["bob@unipd.it"]

    course_titles = ["Algorithms 101", "Data Structures"]
    first_challenge_id: int | None = None
    shortest_route = textwrap.dedent("""\
        You are given a weighted, undirected graph of **city connections**.
        Compute the length of the shortest route between a source city and a
        destination city. If no route exists, return `-1`.

        ### Input

        First line: `n m` — number of cities and roads. Next `m` lines:
        `u v w`. Final line: `src dst`.

        ### Example

        ```
        5 6
        0 1 4
        0 2 1
        2 1 2
        1 3 1
        2 3 5
        3 4 3
        0 4
        ```

        The expected output is `7`.

        ### Constraints

        - 1 ≤ n ≤ 10⁴,  0 ≤ m ≤ 10⁵
        - 0 ≤ w ≤ 10³ · parallel edges allowed
        """)
    for course_title in course_titles:
        course_id = (
            await conn.execute(
                insert(Course)
                .values(title=course_title, description=textwrap.dedent("""\
                        Introduction to Algorithms. This course focuses on

                        - Honing algorithm design skills
                        - Introduce students to basic implementation techniques
                        """))
                .returning(Course.c.id)
            )
        ).scalar_one()
        # Matteo administers everything and Alice teaches it; Bob and Charlie
        # are students. `role` is per course, so this is one row per person per
        # course - and Matteo's `admin` rows are the only thing in the seed that
        # makes "New course" reachable at all.
        await insert_enrollment(
            conn, user_ids[DUMMY_ADMIN], course_id, UserRole.admin
        )
        for email in DUMMY_TEACHERS:
            await insert_enrollment(
                conn, user_ids[email], course_id, UserRole.teacher
            )
        for email in DUMMY_STUDENTS:
            await insert_enrollment(
                conn, user_ids[email], course_id, UserRole.student
            )
        for n in range(3):
            first = first_challenge_id is None
            challenge_id = (
                await conn.execute(
                    insert(Challenge)
                    .values(
                        title=(
                            "Shortest Route Planner"
                            if first
                            else f"{course_title} - Challenge {n + 1}"
                        ),
                        description=(
                            shortest_route
                            if first
                            else f"challenge {n + 1} of {course_title}"
                        ),
                        course_id=course_id,
                        # Listed in the order they are created here; course
                        # staff reorder them with the arrows in the editor.
                        position=n,
                        # A CPython interpreter alone needs well over a few MB,
                        # so these have to be realistic or every run is killed.
                        max_memory_bytes=256 << 20,
                        max_time_s=5.0,
                        max_threads=1,
                        # No evaluator of its own: graded by the bundled
                        # default, exact match.
                        evaluator_path=None,
                    )
                    .returning(Challenge.c.id)
                )
            ).scalar_one()
            if first_challenge_id is None:
                first_challenge_id = challenge_id

    assert first_challenge_id is not None

    # The first three are public samples, downloadable from the challenge page;
    # the rest are hidden. The last one is retired, so the editor shows both
    # states without anyone having to click first. Contents go on the
    # filesystem, not in the database (DESIGN.md, "Test case inputs"), so they
    # are written through `storage`.
    cases = [
        ("5 6\n0 1 4\n0 2 1\n2 1 2\n1 3 1\n2 3 5\n3 4 3\n0 4\n", "7\n", True, True),
        ("4 2\n0 1 5\n2 3 7\n0 3\n", "-1\n", True, True),
        (
            "6 9\n0 1 7\n0 2 9\n0 5 14\n1 2 10\n1 3 15\n2 3 11\n2 5 2\n"
            "3 4 6\n5 4 9\n0 4\n",
            "20\n",
            True,
            True,
        ),
        ("2 1\n0 1 3\n1 0\n", "3\n", False, True),
        ("3 3\n0 1 2\n1 2 2\n0 2 10\n0 2\n", "4\n", False, False),
    ]
    testcase_id: int | None = None
    for order, (test_input, expected, is_sample, is_active) in enumerate(cases):
        input_path = f"challenge_{first_challenge_id}/in_{order}.txt"
        output_path = f"challenge_{first_challenge_id}/out_{order}.txt"
        storage.write(input_path, test_input)
        storage.write(output_path, expected)
        tc_id = (
            await conn.execute(
                insert(TestCase)
                .values(
                    challenge_id=first_challenge_id,
                    execution_order=order,
                    input_path=input_path,
                    expected_output_path=output_path,
                    is_sample=is_sample,
                    is_active=is_active,
                )
                .returning(TestCase.c.id)
            )
        ).scalar_one()
        if testcase_id is None:
            testcase_id = tc_id

    assert testcase_id is not None

    # A plausible solution to the seeded challenge, so the submission page has
    # something worth highlighting in development.
    seeded_source = textwrap.dedent("""\
        import heapq
        import sys


        def shortest_route(n, edges, src, dst):
            \"\"\"Dijkstra over an undirected graph with non-negative weights.\"\"\"
            graph = [[] for _ in range(n)]
            for u, v, w in edges:
                graph[u].append((v, w))
                graph[v].append((u, w))

            dist = [float("inf")] * n
            dist[src] = 0
            queue = [(0, src)]
            while queue:
                d, u = heapq.heappop(queue)
                if u == dst:
                    return d
                if d > dist[u]:
                    continue  # a stale entry, already improved on
                for v, w in graph[u]:
                    if d + w < dist[v]:
                        dist[v] = d + w
                        heapq.heappush(queue, (dist[v], v))
            return -1


        def main():
            data = sys.stdin.read().split()
            n, m = int(data[0]), int(data[1])
            edges = [
                (int(data[2 + 3 * i]), int(data[3 + 3 * i]), int(data[4 + 3 * i]))
                for i in range(m)
            ]
            src, dst = int(data[2 + 3 * m]), int(data[3 + 3 * m])
            print(shortest_route(n, edges, src, dst))


        if __name__ == "__main__":
            main()
        """)

    async def _seed_submission(
        author_id: int,
        filename: str,
        outcome: OutcomeEnum,
        score: float,
        elapsed_s: float | None,
    ) -> None:
        submission_id = (
            await conn.execute(
                insert(Submission)
                .values(
                    user_id=author_id,
                    challenge_id=first_challenge_id,
                    status=SubmissionStatus.done,
                    language="python",
                    filename=filename,
                    source=seeded_source,
                )
                .returning(Submission.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(TestOutcome).values(
                submission_id=submission_id,
                testcase_id=testcase_id,
                outcome=outcome,
                score=score,
                elapsed_s=elapsed_s,
                peak_memory_bytes=12 << 20,
                message="ok" if outcome == OutcomeEnum.success else str(outcome.value),
                stdout="7\n" if outcome == OutcomeEnum.success else "",
                stderr="" if outcome == OutcomeEnum.success else "RuntimeError: boom",
            )
        )

    # A history of attempts for one student, so the submission page and the
    # per-student staff view have something to show.
    await _seed_submission(
        student_id, "first_try.py", OutcomeEnum.runtime_error, 0.0, None
    )
    await _seed_submission(student_id, "route_bfs.py", OutcomeEnum.success, 0.75, 0.412)
    await _seed_submission(
        student_id, "shortest_route.py", OutcomeEnum.success, 1.0, 0.238
    )
    await _seed_submission(
        user_ids["charlie@unipd.it"], "charlie.py", OutcomeEnum.success, 0.5, 0.310
    )

    # A few classmates, so the leaderboard has something to rank. They are not
    # enrolled: `query_challenge_standings` builds its cohort as the enrolled
    # students plus anyone who submitted, so they show up regardless.
    for nickname, filename, elapsed_s in [
        ("bianchi", "dijkstra.py", 0.121),
        ("rossi", "heapq_solution.py", 0.164),
        ("ferrari", "route.py", 0.199),
        ("romano", "planner.py", 0.402),
    ]:
        classmate_id = await _seed_user(conn, f"{nickname}@example.com", nickname)
        await _seed_submission(
            classmate_id, filename, OutcomeEnum.success, 1.0, elapsed_s
        )




########################################################################
# dependency injection
async def get_db():
    async with engine.connect() as conn:
        yield conn

DBDep = Annotated[AsyncConnection, Depends(get_db)]

class NotAuthenticated(Exception):
    """Raised by `get_user` when there is no session behind a request.

    Not an `HTTPException`, because the right answer depends on who is asking:
    a browser wants a redirect, htmx wants a header. See the handler below.
    """

    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


async def get_user(request: Request, db: DBDep) -> schema.User:
    """The signed-in user, or `NotAuthenticated`.

    The cookie holds nothing but the user id, so a renamed - or deleted -
    account takes effect on the very next request.

    The user is also stashed on `request.state`, for the one caller that cannot
    ask for a dependency: `handle_forbidden` renders a whole page, navbar
    included, and this has always run by the time an authorization dependency
    can refuse.
    """
    if DUMMY_AUTH:
        # Development only: there is no sign-in step at all. The address is
        # resolved on every request rather than stashed in the session, and the
        # account is created if it is missing, so this works against an empty
        # database too - with no enrollments, which is exactly what a first
        # real sign-in gets.
        user = await get_or_create_user(db, DUMMY_USER)
        await db.commit()
        request.state.user = user
        return user

    user_id = request.session.get("user_id")
    if user_id is not None:
        row = await query_user(db, user_id)
        if row is not None:
            user = schema.User.model_validate(row)
            request.state.user = user
            return user
        # A session pointing at nobody: in development, typically the other
        # side of a COCCO_RESET_DB.
        request.session.clear()
    raise NotAuthenticated(_return_path(request))


CurrentUser = Annotated[schema.User, Depends(get_user)]


def _return_path(request: Request) -> str:
    """Where to send the visitor once they have signed in.

    Only a plain GET of a page is worth coming back to: replaying a POST after
    a login is not something to attempt, and a polled fragment is not a
    destination - its page is.
    """
    if request.method != "GET" or request.headers.get("HX-Request") == "true":
        return "/"
    return safe_next_path(request.url.path)


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    login_url = str(
        request.url_for("get_login").include_query_params(next=exc.next_path)
    )
    if request.headers.get("HX-Request") == "true":
        # htmx follows a 302 inside the XHR, so a redirect here would splice the
        # login page into whatever div was polling - every two seconds, for as
        # long as the tab stays open. HX-Redirect navigates the browser instead.
        return Response(status_code=204, headers={"HX-Redirect": login_url})
    return RedirectResponse(login_url, status_code=303)


class Forbidden(HTTPException):
    """A 403 that renders as a page.

    Its own class rather than a plain `HTTPException` so the handler below can
    be registered on it alone: FastAPI's defaults still answer everything else,
    and a 404 for an unparseable id does not go through a template.

    `detail` is written for the person reading it, not for a log - it is the
    sentence the page shows.
    """

    def __init__(self, detail: str):
        super().__init__(status_code=403, detail=detail)


@app.exception_handler(Forbidden)
async def handle_forbidden(request: Request, exc: Forbidden):
    """The refusal, as a page.

    Enrollment is the gate on every course, so a 403 is now something an
    ordinary visitor can reach by clicking rather than by typing a URL, and raw
    JSON is not an answer for them. Polled fragments are unharmed: htmx does not
    swap the body of an error response, so a fragment behind a course the viewer
    left simply stops updating.
    """
    return templates.TemplateResponse(
        request=request,
        name="403.jinja",
        status_code=403,
        context={
            "message": exc.detail,
            "title": "Not allowed",
            # Stashed by `get_user`; absent only if something refused before
            # anyone was identified, and `base.jinja` guards the navbar anyway.
            "user": getattr(request.state, "user", None),
        },
    )


########################################################################
# authentication
#
# A visitor proves an address by signing in with Google, and the account is
# created the first time an accepted address comes back. None of these routes
# depends on `CurrentUser`: they are the way in.


def _base_url(request: Request) -> str:
    """The absolute origin Google must redirect back to.

    Configured explicitly when there is a proxy in front, since only the
    deployment knows the public name - and a proxy that terminates TLS makes
    `request.base_url` say `http`, which Google would refuse as a mismatch.
    """
    if CONFIG.base_url:
        return CONFIG.base_url
    return str(request.base_url).rstrip("/")


def _login_page(
    request: Request,
    *,
    next_path: str = "/",
    error: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name="login.jinja",
        status_code=status_code,
        context={
            "title": "Sign in",
            # `base.jinja` guards on this: there is nobody to put in the navbar.
            "user": None,
            "next": next_path,
            "error": error,
            "google_configured": CONFIG.auth.google is not None,
            "allowed_domains": CONFIG.auth.allowed_domains,
        },
    )


def _sign_in(request: Request, user: schema.User, next_path: str):
    # Never carry a prior session over into a new identity - and this is also
    # what drops the OAuth state Authlib left behind, now spent.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(next_path, status_code=303)


def _prune_stale_oauth_state(request: Request) -> None:
    """Drop the leftovers of abandoned sign-ins.

    Authlib stashes one entry per *started* flow in the session and removes
    only the one that finishes, so a visitor who keeps bouncing off the consent
    screen would grow the cookie without bound. Only the flow about to start
    matters.
    """
    for key in [k for k in request.session if k.startswith("_state_google_")]:
        del request.session[key]


@app.get("/login")
async def get_login(request: Request, next: str = "/"):
    next_path = safe_next_path(next)
    if DUMMY_AUTH:
        # There is nothing to click: `get_user` signs every request in.
        return RedirectResponse(next_path, status_code=303)
    return _login_page(request, next_path=next_path)


@app.get("/auth/google")
async def get_auth_google(request: Request, next: str = "/"):
    """Hand off to Google.

    Authlib puts the `state`, the `nonce` and the PKCE verifier in the session
    and returns the redirect to the consent screen.
    """
    next_path = safe_next_path(next)
    if DUMMY_AUTH:
        return RedirectResponse(next_path, status_code=303)

    client = google_client()
    if client is None:
        return _login_page(
            request,
            next_path=next_path,
            error="Google sign-in is not configured on this server.",
            status_code=503,
        )

    # The destination rides in the session, not in the redirect URI: that URI is
    # matched character for character against the console registration, so it
    # cannot carry a query string. Unlike an emailed link this is the same
    # browser throughout, which makes the session the natural place for it.
    request.session["login_next"] = next_path
    _prune_stale_oauth_state(request)

    params = {}
    if CONFIG.auth.google is not None and CONFIG.auth.google.hd:
        # A hint for the account chooser, never a check: `hd` is absent for
        # consumer accounts, and the gate is `is_allowed_email` on the address.
        params["hd"] = CONFIG.auth.google.hd

    try:
        return await client.authorize_redirect(
            request, google_redirect_uri(_base_url(request)), **params
        )
    except Exception:
        # Typically the metadata document could not be fetched. A machine that
        # is offline should say so rather than raise a 500.
        logger.exception("could not start the Google sign-in flow")
        return _login_page(
            request,
            next_path=next_path,
            error="Could not reach Google right now. Please try again.",
            status_code=502,
        )


@app.get("/auth/google/callback")
async def get_auth_google_callback(request: Request, db: DBDep):
    """Finish the flow: verify the ID token, check the domain, sign in.

    Every failure lands on the login page with one sentence, never a traceback:
    the visitor arrives here from Google, so a 500 would be a dead end.
    """
    # Read before `_sign_in` clears the session. A callback that arrives
    # without one - a bookmarked URL, a cleared cookie - lands on the root.
    next_path = safe_next_path(request.session.get("login_next"))

    client = google_client()
    if client is None:
        return _login_page(
            request,
            error="Google sign-in is not configured on this server.",
            status_code=503,
        )

    try:
        token = await client.authorize_access_token(request)
    except OAuthError as err:
        # Covers the visitor pressing Cancel (`access_denied`), a mismatched or
        # expired `state`, and a token endpoint that refused the code. Keep the
        # clause this narrow: widening it would fold a failed state check into
        # a success path, which is the one bug here that fails silently.
        logger.info("a Google sign-in did not complete: %s", err.error)
        return _login_page(
            request,
            next_path=next_path,
            error="That sign-in did not complete. Please try again.",
            status_code=400,
        )

    # Authlib has already verified the ID token's signature, issuer, audience
    # and nonce; `userinfo` is its decoded claims.
    claims = token.get("userinfo") or {}
    try:
        email = normalize_email(claims.get("email") or "")
    except ValueError:
        return _login_page(
            request,
            next_path=next_path,
            error="Google did not return an email address.",
            status_code=400,
        )

    if claims.get("email_verified") not in (True, "true"):
        # Google says the address is unconfirmed, so it proves nothing. Matched
        # against the two affirmative spellings rather than tested for
        # truthiness: the string "false" is truthy, and this is the kind of
        # mistake that would never fail visibly.
        return _login_page(
            request,
            next_path=next_path,
            error="That Google account's email address is not verified.",
            status_code=403,
        )

    if not is_allowed_email(email, CONFIG.auth.allowed_domains):
        logger.info("refused a sign-in from outside the allowed domains: %s", email)
        domains = ", ".join("@" + d for d in CONFIG.auth.allowed_domains)
        return _login_page(
            request,
            next_path=next_path,
            error=(
                f"{email} is not an institutional address. "
                f"Sign in with your {domains} account."
            ),
            status_code=403,
        )

    user = await get_or_create_user(db, email)
    await db.commit()
    return _sign_in(request, user, next_path)


@app.post("/logout")
async def post_logout(request: Request):
    request.session.clear()
    if DUMMY_AUTH:
        # Nothing to sign out of: `get_user` signs the next request straight
        # back in. Bounce to the root rather than to a login page that would
        # only redirect there anyway.
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login", status_code=303)


async def require_admin(db: DBDep, user: CurrentUser) -> schema.User:
    """Authorize creating a course, or raise 403.

    The only permission in the app that names no course, because the course does
    not exist yet: `role` lives on `enrollment`, so the closest the schema comes
    to a site-wide administrator is holding `admin` somewhere, and that is what
    `user_is_admin` asks. Deliberately narrower than `require_course_editor` - a
    teacher runs a course they were given, an admin brings new ones into being.

    Returns the user, so a route that needs the creator does not ask twice.
    Hiding the button on the home page is an affordance, not this check.
    """
    if not await user_is_admin(db, user.id):
        raise Forbidden("Only an administrator can create a course.")
    return user


Admin = Annotated[schema.User, Depends(require_admin)]


async def require_course_editor(
    course_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """Authorize course staff on `course_id`, or raise 403.

    Keyed on the course rather than on a challenge, so it also answers the one
    question a challenge cannot: whether this person may create one here.
    Hiding a button in the template is a UI affordance, not access control.
    """
    role = await query_course_role(db, user_id=user.id, course_id=course_id)
    if role is None or role not in EDITOR_ROLES:
        raise Forbidden("Only this course's staff can do that.")
    return role


CourseEditor = Annotated[UserRole, Depends(require_course_editor)]


async def require_course_member(
    course_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """Authorize anyone enrolled on `course_id`, of any role, or raise 403.

    This is the gate on a course's contents. `EDITOR_ROLES` is a subset of
    "enrolled", so staff pass it without a second check, and it is deliberately
    the same `query_course_role` `require_course_editor` uses: the role is per
    course, so the course in the path is the key.

    What it does *not* guard is the course page itself - `get_course` shows a
    non-member the title, the description and the way in - nor the home page,
    which has to go on listing every course or nobody could find one to enroll
    on, nor `post_course_enroll`, which is that way in.
    """
    role = await query_course_role(db, user_id=user.id, course_id=course_id)
    if role is None:
        raise Forbidden("You are not enrolled on this course.")
    return role


CourseMember = Annotated[UserRole, Depends(require_course_member)]


async def require_challenge_viewer(
    challenge_id: int, course_id: int, db: DBDep, role: CourseMember
) -> UserRole:
    """Authorize reading `challenge_id` under `course_id`, or raise 403 - or 404.

    `require_challenge_editor` with the membership gate in place of the staff
    one, and for the same reason: the role comes from the course in the path, so
    the challenge in the path has to be checked against it. Otherwise a student
    of one course could name their own course in front of another course's
    challenge and read it. A mismatched pair is a URL for a challenge that does
    not exist under that course, hence 404.
    """
    if not await challenge_in_course(db, challenge_id, course_id):
        raise HTTPException(status_code=404)
    return role


ChallengeViewer = Annotated[UserRole, Depends(require_challenge_viewer)]


async def require_challenge_member(
    challenge_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """The same gate, for the routes whose URL names only a challenge.

    Keyed on the challenge because there is nothing else to key on: these are
    the fragments and the sample download, whose paths carry no `course_id`.
    That is not the mistake `require_challenge_editor` avoids - there the course
    is in the path too, and staff of one course naming it in front of another's
    challenge is a real attack. With one id there is no second key to disagree
    with, and the challenge determines its course.
    """
    role = await query_user_role(db, user_id=user.id, challenge_id=challenge_id)
    if role is None:
        raise Forbidden("You are not enrolled on the course this challenge belongs to.")
    return role


ChallengeMember = Annotated[UserRole, Depends(require_challenge_member)]


async def require_challenge_editor(
    challenge_id: int, course_id: int, db: DBDep, role: CourseEditor
) -> UserRole:
    """Authorize an edit of `challenge_id`, or raise 403 - or 404.

    The role comes from the course in the path, so the challenge in the path
    has to be checked against it rather than assumed: otherwise staff of one
    course could name their own course in front of someone else's challenge
    and edit it. A mismatched pair is a URL for a challenge that does not
    exist under that course, hence 404 rather than 403.
    """
    if not await challenge_in_course(db, challenge_id, course_id):
        raise HTTPException(status_code=404)
    return role


ChallengeEditor = Annotated[UserRole, Depends(require_challenge_editor)]


async def authorize_submission(
    db: AsyncConnection, user: schema.User, submission
) -> bool:
    """Let the viewer see a submission, or raise 403. Returns True for staff.

    Not a dependency like `require_challenge_editor`, because it needs the
    submission row first and because a student *is* allowed here: the rule is
    that you may read your own work, and course staff may read anyone's.
    """
    role = await query_user_role(
        db, user_id=user.id, challenge_id=submission["challenge_id"]
    )
    is_staff = role in EDITOR_ROLES
    if not is_staff and submission["user_id"] != user.id:
        raise Forbidden("That submission is not yours.")
    return is_staff


# Fields of an outcome that are free text produced by the run rather than by the
# judge's own bookkeeping. `stdout`/`stderr` are the submitted program's output,
# and `message` comes from the challenge's evaluator, which has just read both
# the hidden input and the expected output - the bundled diff.py puts the
# expected output in it verbatim. On a hidden test case all three would hand a
# student the test data, so they are only shown for samples, or to course staff.
_OUTCOME_FREE_TEXT = ("message", "stdout", "stderr")


def visible_outcomes(outcomes, is_staff: bool):
    """The outcomes as this viewer may see them.

    The verdict, the score and the resource usage are always shown - a student
    has to know which test failed. Only the free text is redacted, and only on
    test cases that are not public samples.
    """
    if is_staff:
        return outcomes
    return [
        outcome
        if outcome["is_sample"]
        else {**outcome, **{field: None for field in _OUTCOME_FREE_TEXT}}
        for outcome in outcomes
    ]


@app.on_event("startup")
async def on_startup():
    # Resetting drops every table and reseeds the dummy data, so it must not
    # happen on an ordinary boot: `fastapi dev` reloads would wipe every
    # submission made while testing.
    await create_db_and_tables(dummy=os.environ.get("COCCO_RESET_DB") == "1")


@app.get("/")
async def index(request: Request, db: DBDep, user: CurrentUser, q: str = ""):
    courses = await query_courses(db, user_id=user.id, q=q)
    return templates.TemplateResponse(
        request=request,
        name="index-logged-in.jinja",
        context={
            "courses": courses,
            "q": q,
            "title": "COCCO: Coding Challenges Companion",
            "user": user,
            "nav": "courses",
            # Only an administrator is offered "New course". The route behind
            # the button checks for itself - see `require_admin`.
            "can_create_course": await user_is_admin(db, user.id),
        },
    )


async def _render_courses(
    request: Request, db: AsyncConnection, user: schema.User, q: str
):
    """Both halves of the courses list. Shared by the filter box's GET and by
    `post_course_enroll`, so the list a click leaves behind is the one the box
    would have produced."""
    courses = await query_courses(db, user_id=user.id, q=q)
    return templates.TemplateResponse(
        request=request,
        name="f-courses.jinja",
        context={"courses": courses, "q": q, "user": user},
    )


@app.get("/f/courses")
async def get_fragment_courses(request: Request, db: DBDep, user: CurrentUser, q: str = ""):
    """Both halves of the courses list, re-rendered as the filter box is typed
    in. The page inlines this same fragment on first paint, so the filtered
    list and the initial one cannot drift apart.

    No authorization beyond `CurrentUser`: this is exactly what `index` shows,
    and every course is visible to every signed-in user."""
    return await _render_courses(request, db, user, q)


########################################################################
# Creating a course. Administrators only, through `Admin`.
#
# Declared here, above `get_course`, and that is load-bearing for the same
# reason the course editor is declared above `get_challenge`: `/courses/{course_id}`
# matches any single segment, so a `/courses/new` declared after it would be
# routed there and rejected as an unparseable id before this ever ran.


@app.get("/courses/new")
async def get_course_new(request: Request, user: Admin):
    """The empty course form."""
    return templates.TemplateResponse(
        request=request,
        name="course-new.jinja",
        context={"user": user, "title": "New course"},
    )


@app.post("/courses/new")
async def post_course_new(
    request: Request,
    db: DBDep,
    user: Admin,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
):
    """Create the course, enroll its creator, and send the browser to the editor."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    title = title.strip()
    if not title:
        return alert("A course needs a title.")

    course_id = await insert_course(db, title=title, description=description)
    # Being an admin somewhere does not make one staff here: without this row
    # the creator could not open the editor of the course they just made.
    await insert_enrollment(db, user.id, course_id, UserRole.teacher)
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    # A 204 with HX-Redirect rather than a 303, for the reason spelled out in
    # `post_challenge_new`: htmx would splice a whole page into the alert div
    # this form targets. The editor is the right landing place - the new course
    # has no challenges yet, and that is where they are added.
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(
                request.url_for("get_course_edit", course_id=course_id)
            )
        },
    )


@app.get("/courses/{course_id}")
async def get_course(course_id: int, request: Request, db: DBDep, user: CurrentUser):
    """The course, or - for someone not enrolled on it - a preview of it.

    The one page under `/courses/` with no membership gate, and deliberately:
    the home page lists every course, so this is where a link from it lands, and
    it is where the `Enroll!` button makes the most sense. A non-member sees the
    title and the description, which the home page's card already shows
    truncated, and nothing else. The challenge list is not merely hidden in the
    template - `query_course_info` is never run for them.
    """
    role = await query_course_role(db, user_id=user.id, course_id=course_id)
    if role is None:
        course = await query_course(db, course_id)
        if course is None:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="course.jinja",
            context={
                "course": course,
                "course_id": course_id,
                "title": course.title,
                "user": user,
                "can_edit": False,
                "is_member": False,
            },
        )

    # The viewer is part of the query: each challenge card says whether *this*
    # person has already submitted to it.
    course = await query_course_info(db, course_id, user_id=user.id)
    if course is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="course.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "title": course["title"],
            "user": user,
            # Only staff are offered the Edit and New challenge buttons. The
            # routes behind them check for themselves - see
            # `require_course_editor`.
            "can_edit": role in EDITOR_ROLES,
            "is_member": True,
        },
    )


@app.post("/courses/{course_id}/enroll")
async def post_course_enroll(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    q: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    """Enroll the viewer on a course, from either `Enroll!` button.

    Authorization is `CurrentUser` and deliberately nothing else: this is the
    way in, and a membership gate here would lock the door from the inside. It
    is the only route under a course that a non-member may call, which is why
    the enrollment it writes has to be the one thing it can write - the role is
    not a parameter, see `insert_self_enrollment`, which fixes it at `student`
    and leaves an existing enrollment alone, so this can neither promote anyone
    nor demote a teacher who clicks it. Who may sign in at all is
    `is_allowed_email`; who may sign up for *this* course is, for now, anyone
    who can.

    Two callers, so two answers. From the home page it renders the whole list,
    both halves, because the point of the click is that the course moves from
    "Other courses" up into "Your courses"; `q` comes back with it so enrolling
    under a typed filter does not silently reset the list. From a course
    preview there is no list to re-render - the page has to become the course
    page - so that form sends `next` and gets the `HX-Redirect` idiom
    `post_course_new` uses. `next` is filtered through `safe_next_path` like
    every other destination in the app: it arrives in a request, so it is not
    trusted to be a path on this site.
    """
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)

    await insert_self_enrollment(db, user_id=user.id, course_id=course_id)
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    if next:
        return Response(
            status_code=204, headers={"HX-Redirect": safe_next_path(next)}
        )
    return await _render_courses(request, db, user, q)


########################################################################
# The course editor: the course's own description, and the order its challenges
# are listed in. Course staff only, through `CourseEditor`.
#
# Declared here, above `get_challenge`, and that is load-bearing: its path is
# `/courses/{course_id}/{challenge_id}`, whose `{challenge_id}` matches any
# single segment, so a `/courses/1/edit` declared after it would be routed
# there and rejected as an unparseable id before this ever ran.


async def _render_course_challenges(
    request: Request, db: DBDep, course_id: int, user: schema.User
):
    """The reorderable challenge list. Every move returns this whole fragment,
    so the cards always sit in the order the database has them."""
    challenges = await query_course_challenges(db, course_id, user_id=user.id)
    return templates.TemplateResponse(
        request=request,
        name="f-course-challenges.jinja",
        context={
            "challenges": challenges,
            "course_id": course_id,
            "user": user,
        },
    )


@app.get("/courses/{course_id}/edit")
async def get_course_edit(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
):
    course = await query_course_info(db, course_id, user_id=user.id)
    if course is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="course-edit.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "title": f"Editing {course['title']}",
            "user": user,
        },
    )


@app.post("/courses/{course_id}/edit")
async def post_course_edit(
    course_id: int,
    request: Request,
    db: DBDep,
    role: CourseEditor,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
):
    """Save the course's title and description."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    title = title.strip()
    if not title:
        return alert("A course needs a title.")

    await update_course(db, course_id, title=title, description=description)
    await db.commit()
    return alert("Saved.", ok=True)


@app.post("/courses/{course_id}/challenges/{challenge_id}/move")
async def post_challenge_move(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeEditor,
    direction: Annotated[str, Form()],
):
    """Move a challenge one place up or down the course page. Arrows rather
    than drag-and-drop, exactly as `post_testcase_move`: no JS build step, and
    this is a list of integers.

    Unlike that one it hands the whole reordered course to the repository
    rather than naming two rows to exchange - see `reorder_challenges` for why
    a swap is not enough here.
    """
    if direction not in ("up", "down"):
        raise HTTPException(status_code=404)

    ordered = await query_ordered_challenge_ids(db, course_id)
    index = next((i for i, cid in enumerate(ordered) if cid == challenge_id), None)
    if index is None:
        raise HTTPException(status_code=404)

    neighbour = index - 1 if direction == "up" else index + 1
    if not 0 <= neighbour < len(ordered):
        # Already at the end; nothing to do rather than an error
        return await _render_course_challenges(request, db, course_id, user)

    ordered[index], ordered[neighbour] = ordered[neighbour], ordered[index]
    await reorder_challenges(db, ordered)
    await db.commit()
    return await _render_course_challenges(request, db, course_id, user)

@app.get("/courses/{course_id}/{challenge_id}")
async def get_challenge(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeViewer,
    as_: Annotated[str | None, Query(alias="as")] = None,
):
    """The challenge, in whichever of its two views fits the viewer.

    Course staff get `challenge-staff.jinja`: the statement plus the whole
    class's standings, and no submit form. `?as=student` gives them the page a
    student sees, which is the only way to check how the statement reads.

    Enrollment is the gate: a challenge belongs to the class it was set for.
    """
    challenge = await query_challenge(db, user_id=user.id, challenge_id=challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404)
    can_edit = challenge["viewer_role"] in EDITOR_ROLE_NAMES
    staff_view = can_edit and as_ != "student"
    return templates.TemplateResponse(
        request=request,
        name="challenge-staff.jinja" if staff_view else "challenge.jinja",
        context={
            "user": user,
            "course_id": course_id,
            "challenge": challenge,
            "title": challenge["title"],
            "can_edit": can_edit,
            # Staff looking at the student page got here on purpose, so offer
            # them the way back.
            "viewing_as_student": can_edit and not staff_view,
        },
    )


@app.post("/courses/{course_id}/{challenge_id}/submissions")
async def post_submission(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeViewer,
    solution: Annotated[UploadFile, File()],
):
    """Enqueue an uploaded solution. The judge process picks it up from there.

    Behind the same gate as reading the challenge, so every submission on record
    is by someone who was enrolled when they made it.

    Errors are reported as a 200 with an error alert rather than a 4xx: htmx
    does not swap the response of an error status by default, and the fragment
    is the whole point of the request.
    """

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-submit-result.jinja",
            context={"ok": ok, "message": message},
        )

    filename = os.path.basename(solution.filename or "")
    if not filename:
        return alert("No file was uploaded.")

    contents = await solution.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return alert(
            f"That file is {len(contents) / 1024:.0f} KB — the limit is 1 MB."
        )

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    language = EXTENSION_TO_LANGUAGE.get(extension)
    if language is None:
        known = ", ".join(sorted("." + e for e in EXTENSION_TO_LANGUAGE))
        return alert(f"Cannot tell the language of {filename!r}. Accepted: {known}.")

    try:
        source = contents.decode("utf-8")
    except UnicodeDecodeError:
        return alert(f"{filename} is not valid UTF-8 text.")

    await insert_submission(
        db,
        user_id=user.id,
        challenge_id=challenge_id,
        language=language,
        filename=filename,
        source=source,
    )
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    return alert(
        f"{filename} was queued as a {language} submission. "
        "Results appear below as the judge works through it.",
        ok=True,
    )


@app.get("/f/submissions/{challenge_id}")
async def get_fragment_submissions(
    challenge_id: int, request: Request, db: DBDep, user: CurrentUser,
    role: ChallengeMember,
):
    submissions = await query_user_submissions(db, user_id=user.id, challenge_id=challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-submissions.jinja",
        context={
            "submissions": submissions or []
        },
    )


@app.get("/f/leaderboard/{challenge_id}")
async def get_fragment_leaderboard(
    challenge_id: int, request: Request, db: DBDep, user: CurrentUser,
    role: ChallengeMember,
):
    ranking = await query_challenge_ranking(db, challenge_id)
    if ranking is None:
        raise HTTPException(status_code=404)
    entries = await query_challenge_leaderboard(
        db, challenge_id=challenge_id, ranking=ranking
    )
    return templates.TemplateResponse(
        request=request,
        name="f-leaderboard.jinja",
        context={
            "entries": entries or [],
            "ranking": ranking.value,
            "user": user,
        },
    )


@app.get("/challenges/{challenge_id}/samples/{testcase_id}/{kind}")
async def get_sample_file(
    challenge_id: int, testcase_id: int, kind: str, db: DBDep, user: CurrentUser,
    role: ChallengeMember,
):
    """Serve the input or expected output of a *public* test case.

    `query_sample_testcase` returns nothing for hidden test cases, so those
    404 rather than leaking the judge's data. The viewer must additionally be
    enrolled in the course - a sample belongs to the class it was set for, and
    an id is easy to guess - which is now `ChallengeMember`, the same gate as
    the challenge page the download link sits on.
    """
    if kind not in ("input", "output"):
        raise HTTPException(status_code=404)
    testcase = await query_sample_testcase(db, challenge_id, testcase_id)
    if testcase is None:
        raise HTTPException(status_code=404)

    relative = (
        testcase.input_path if kind == "input" else testcase.expected_output_path
    )
    path = storage.resolve(relative)
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="text/plain",
        filename=f"sample{testcase.execution_order + 1}_{kind}.txt",
    )


########################################################################
# One submission. Reached from the submission table on both the student's own
# challenge page and the staff view of a single student, which are the same
# fragment. Not nested under the course: the submission id already determines
# its challenge and course, so a course_id in the path would only be a segment
# to get wrong.


@app.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: int, request: Request, db: DBDep, user: CurrentUser
):
    """A submission's source, its compiler output and its per-test outcomes."""
    submission = await query_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status_code=404)
    is_staff = await authorize_submission(db, user, submission)
    viewer_is_author = submission["user_id"] == user.id

    course_id = submission["course_id"]
    challenge_id = submission["challenge_id"]
    if viewer_is_author:
        back_url = request.url_for(
            "get_challenge", course_id=course_id, challenge_id=challenge_id
        )
        back_label = f"← Back to {submission['challenge_title']}"
    else:
        back_url = request.url_for(
            "get_challenge_student",
            course_id=course_id,
            challenge_id=challenge_id,
            student_id=submission["user_id"],
        )
        back_label = f"← Back to {submission['author_nickname']}'s attempts"

    return templates.TemplateResponse(
        request=request,
        name="submission.jinja",
        context={
            "user": user,
            "submission": submission,
            "outcomes": visible_outcomes(submission["outcomes"], is_staff),
            "is_staff": is_staff,
            "viewer_is_author": viewer_is_author,
            "back_url": back_url,
            "back_label": back_label,
            "title": f"#{submission['attempt']} · {submission['challenge_title']}",
        },
    )


@app.get("/f/submission/{submission_id}")
async def get_fragment_submission_results(
    submission_id: int, request: Request, db: DBDep, user: CurrentUser
):
    """The results half of the submission page, polled while it is still being
    judged. Deliberately a different query from the page's: the source never
    changes and there is no point shipping it every two seconds."""
    submission = await query_submission_outcomes(db, submission_id)
    if submission is None:
        raise HTTPException(status_code=404)
    is_staff = await authorize_submission(db, user, submission)
    return templates.TemplateResponse(
        request=request,
        name="f-submission-results.jinja",
        context={
            "submission": submission,
            "outcomes": visible_outcomes(submission["outcomes"], is_staff),
            "is_staff": is_staff,
        },
    )


########################################################################
# The staff view of a challenge. Like the editor below, every route here
# depends on ChallengeEditor - these pages show one student's work to another
# person, so the check is access control and not just an affordance.


@app.get("/f/standings/{challenge_id}")
async def get_fragment_standings(
    challenge_id: int,
    course_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    standings = await query_challenge_standings(db, challenge_id=challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-standings.jinja",
        context={
            "standings": standings or [],
            "course_id": course_id,
            "challenge_id": challenge_id,
        },
    )


@app.get("/f/students/{course_id}/{challenge_id}/{student_id}")
async def get_fragment_student_submissions(
    course_id: int,
    challenge_id: int,
    student_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """One student's attempts, for the staff page. Same query and same fragment
    as the student's own history - only the user id differs.

    The course is in the path because the authorization the fragment inherits
    is the course's, not the challenge's.
    """
    submissions = await query_user_submissions(
        db, user_id=student_id, challenge_id=challenge_id
    )
    return templates.TemplateResponse(
        request=request,
        name="f-submissions.jinja",
        context={
            "submissions": submissions or [],
            "empty_message": "This student has not submitted anything yet.",
        },
    )


@app.get("/courses/{course_id}/{challenge_id}/students/{student_id}")
async def get_challenge_student(
    course_id: int,
    challenge_id: int,
    student_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeEditor,
):
    """Everything one student has handed in for one challenge."""
    challenge = await query_challenge(db, user_id=user.id, challenge_id=challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404)
    student = await query_user(db, student_id)
    if student is None:
        raise HTTPException(status_code=404)

    # Only people the standings would list: someone who has submitted, or a
    # student enrolled on the course. Anyone else is a stranger to this
    # challenge and their name is none of this page's business.
    enrolled = await query_user_role(db, user_id=student_id, challenge_id=challenge_id)
    submissions = await query_user_submissions(
        db, user_id=student_id, challenge_id=challenge_id
    )
    if enrolled is None and submissions is None:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="challenge-student.jinja",
        context={
            "user": user,
            "course_id": course_id,
            "challenge": challenge,
            "student": student,
            "title": f"{student.nickname} · {challenge['title']}",
        },
    )


@app.post("/courses/{course_id}/{challenge_id}/regrade")
async def post_regrade(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """Discard every outcome of this challenge and requeue every submission.

    Refused while a judge is mid-run: that run would insert the outcomes of the
    old grading after the reset and mark its submission done, leaving exactly
    one stale score with nothing to mark it as stale. The window is a few
    seconds, so asking staff to press the button again is enough.
    """

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    running = await count_running_submissions(db, challenge_id)
    if running:
        return alert(
            f"A judge is running {running} submission"
            f"{'' if running == 1 else 's'} of this challenge right now. "
            "Nothing was changed — try again in a moment."
        )

    requeued = await regrade_challenge(db, challenge_id)
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    if not requeued:
        return alert("There is nothing to regrade: nobody has submitted yet.", ok=True)
    return alert(
        f"{requeued} submission{'' if requeued == 1 else 's'} requeued on the "
        "active test cases. Scores reappear in the table below as the judge "
        "works through the queue.",
        ok=True,
    )


########################################################################
# Creating a challenge. Course staff only - and course-scoped, because there
# is no challenge yet to hang the permission on.

# What a new challenge starts as. The limits are the seed's, and they have to
# be realistic: a CPython interpreter alone needs well over a few MB, so a
# tighter default would kill the first submission ever made.
NEW_CHALLENGE_DEFAULTS = {
    "title": "",
    "description": "",
    "max_time_s": 5.0,
    "max_memory_bytes": 256 << 20,
    "max_threads": 1,
    "ranking": LeaderboardRanking.running_time.value,
}


def _challenge_fields(
    description: str,
    max_time_s: float,
    max_memory_mb: float,
    max_threads: int,
    ranking: str,
) -> tuple[dict | None, str | None]:
    """The columns a challenge form writes, or the message explaining why not.

    The create and the edit form are the same form - `_challenge-form.jinja` -
    so they are validated once, here, and the caller only decides what to do
    with the result.
    """
    if max_time_s <= 0 or max_memory_mb <= 0 or max_threads < 1:
        return None, "Limits must be positive."

    try:
        criterion = LeaderboardRanking(ranking)
    except ValueError:
        return None, "Unknown leaderboard ranking."

    return {
        "description": description,
        "max_time_s": max_time_s,
        "max_memory_bytes": int(max_memory_mb * 1024 * 1024),
        "max_threads": max_threads,
        "ranking": criterion,
    }, None


@app.get("/courses/{course_id}/challenges/new")
async def get_challenge_new(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
):
    """The empty challenge form. Same form the editor shows, plus a title."""
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="challenge-new.jinja",
        context={
            "user": user,
            "course": course,
            "course_id": course_id,
            "challenge": NEW_CHALLENGE_DEFAULTS,
            "title": f"New challenge in {course.title}",
        },
    )


@app.post("/courses/{course_id}/challenges/new")
async def post_challenge_new(
    course_id: int,
    request: Request,
    db: DBDep,
    role: CourseEditor,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
    max_time_s: Annotated[float, Form()],
    max_memory_mb: Annotated[float, Form()],
    max_threads: Annotated[int, Form()],
    ranking: Annotated[str, Form()],
):
    """Create the challenge and send the browser to its editor."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    title = title.strip()
    if not title:
        return alert("A challenge needs a title.")

    fields, error = _challenge_fields(
        description, max_time_s, max_memory_mb, max_threads, ranking
    )
    if fields is None:
        return alert(error or "Invalid challenge.")

    challenge_id = await insert_challenge(db, course_id=course_id, title=title, **fields)
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    # A 204 with HX-Redirect rather than a 303: htmx follows a redirect inside
    # the XHR, which would splice the whole editor page into the little alert
    # div this form targets. The editor is the right landing place - the new
    # challenge still has no test cases and no evaluator.
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(
                request.url_for(
                    "get_challenge_edit",
                    course_id=course_id,
                    challenge_id=challenge_id,
                )
            )
        },
    )


########################################################################
# The challenge editor. Every route here depends on ChallengeEditor, which
# 403s anyone who is not staff on the course named in the URL, and 404s a
# challenge that does not belong to it.


def _testcase_dir(challenge_id: int) -> str:
    return f"challenge_{challenge_id}"


async def _render_testcases(request: Request, db: DBDep, course_id: int, challenge_id: int,
                            message: str | None = None, ok: bool = True):
    """The test case list fragment. Every mutation returns this, so the table
    always reflects what is actually in the database."""
    testcases = await query_challenge_testcases(db, challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-testcases.jinja",
        context={
            "testcases": testcases,
            "course_id": course_id,
            "challenge_id": challenge_id,
            "message": message,
            "ok": ok,
        },
    )


@app.get("/courses/{course_id}/{challenge_id}/edit")
async def get_challenge_edit(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeEditor,
):
    challenge = await query_challenge(db, user_id=user.id, challenge_id=challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404)
    testcases = await query_challenge_testcases(db, challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="challenge-edit.jinja",
        context={
            # The evaluator card is an included fragment, so the page has to
            # carry the same context the fragment's own route builds.
            **await _evaluator_context(db, course_id, challenge_id),
            "user": user,
            "challenge": challenge,
            "testcases": testcases,
            "title": f"Editing {challenge['title']}",
        },
    )


@app.post("/courses/{course_id}/{challenge_id}/edit")
async def post_challenge_edit(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    description: Annotated[str, Form()],
    max_time_s: Annotated[float, Form()],
    max_memory_mb: Annotated[float, Form()],
    max_threads: Annotated[int, Form()],
    ranking: Annotated[str, Form()],
):
    """Save the problem statement, the sandbox limits and the leaderboard."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    fields, error = _challenge_fields(
        description, max_time_s, max_memory_mb, max_threads, ranking
    )
    if fields is None:
        return alert(error or "Invalid challenge.")

    await update_challenge(db, challenge_id, **fields)
    await db.commit()
    return alert("Saved.", ok=True)


########################################################################
# The challenge's evaluator: the script that turns a program's output into a
# score. NULL means the bundled default; anything else is a file course staff
# uploaded, living beside the challenge's test data rather than inside it.


def _evaluator_dir(challenge_id: int) -> str:
    # A sibling of `_testcase_dir`, not a directory inside it: a test case file
    # may itself be named "evaluator", and then the two would disagree about
    # whether that name is a file or a directory. Test case paths never contain
    # a slash in the name, so these two trees cannot collide.
    return f"evaluator_{challenge_id}"


async def _evaluator_context(
    db: DBDep, course_id: int, challenge_id: int
) -> dict:
    """What `f-evaluator.jinja` needs, whether it is served on its own or
    included by the editor page."""
    path = await query_evaluator_path(db, challenge_id)
    language = None
    if path is not None:
        language = EXTENSION_TO_LANGUAGE.get(
            path.rsplit(".", 1)[-1].lower() if "." in path else ""
        )
    return {
        "course_id": course_id,
        "challenge_id": challenge_id,
        # None means the bundled default, which is not in storage and has no
        # path to show.
        "evaluator_name": None if path is None else path.rsplit("/", 1)[-1],
        "evaluator_language": language,
        "bundled": BUNDLED,
    }


async def _render_evaluator(
    request: Request,
    db: DBDep,
    course_id: int,
    challenge_id: int,
    message: str | None = None,
    ok: bool = True,
):
    """The evaluator card's fragment. Every mutation returns this, so what is
    shown is always what the database says will grade the next submission."""
    context = await _evaluator_context(db, course_id, challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-evaluator.jinja",
        context={**context, "message": message, "ok": ok},
    )


async def _replace_evaluator(
    db: DBDep, challenge_id: int, new_path: str, contents: bytes
) -> None:
    """Point the challenge at a new evaluator, and drop the file it replaces.

    The old file goes only after the new path is committed, so a failed commit
    cannot leave the database naming a script that is no longer on disk.
    """
    previous = await query_evaluator_path(db, challenge_id)
    storage.save_stream(new_path, io.BytesIO(contents))
    await update_challenge(db, challenge_id, evaluator_path=new_path)
    await db.commit()
    if previous is not None and previous != new_path:
        storage.delete(previous)


@app.post("/courses/{course_id}/{challenge_id}/evaluator")
async def post_evaluator(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    evaluator_file: Annotated[UploadFile, File()],
):
    """Upload a script to grade this challenge with."""

    async def refuse(message: str):
        return await _render_evaluator(
            request, db, course_id, challenge_id, message, ok=False
        )

    try:
        name = safe_filename(evaluator_file.filename or "")
    except ValueError as e:
        return await refuse(str(e))

    # Checked before the read, so a huge upload is refused rather than
    # buffered. `size` may be absent, hence the second check below.
    if evaluator_file.size is not None and evaluator_file.size > MAX_EVALUATOR_BYTES:
        return await refuse(
            f"That file is {evaluator_file.size / 1024:.0f} KB — the limit is "
            f"{MAX_EVALUATOR_BYTES // 1024} KB. An evaluator is a script, not "
            "test data."
        )

    contents = await evaluator_file.read()
    if len(contents) > MAX_EVALUATOR_BYTES:
        return await refuse(
            f"That file is {len(contents) / 1024:.0f} KB — the limit is "
            f"{MAX_EVALUATOR_BYTES // 1024} KB. An evaluator is a script, not "
            "test data."
        )

    # Load-bearing, not cosmetic: the extension is the only thing that tells
    # the judge how to compile and run the script, so a name it cannot read is
    # refused here rather than becoming an evaluator_error on every submission.
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    language = EXTENSION_TO_LANGUAGE.get(extension)
    if language is None:
        known = ", ".join(sorted("." + e for e in EXTENSION_TO_LANGUAGE))
        return await refuse(
            f"Cannot tell the language of {name!r}. Accepted: {known}."
        )

    try:
        contents.decode("utf-8")
    except UnicodeDecodeError:
        return await refuse(f"{name} is not valid UTF-8 text.")

    await _replace_evaluator(
        db, challenge_id, f"{_evaluator_dir(challenge_id)}/{name}", contents
    )
    return await _render_evaluator(
        request,
        db,
        course_id,
        challenge_id,
        f"{name} will grade submissions from now on, as {language}.",
        ok=True,
    )


@app.post("/courses/{course_id}/{challenge_id}/evaluator/bundled")
async def post_evaluator_bundled(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    name: Annotated[str, Form()],
):
    """Copy one of the bundled evaluators in, as a starting point to edit."""
    try:
        # `bundled_path` refuses anything that is not a key of BUNDLED, so a
        # request value never becomes a path.
        source = bundled_path(name)
    except KeyError:
        return await _render_evaluator(
            request, db, course_id, challenge_id, "Unknown evaluator.", ok=False
        )

    await _replace_evaluator(
        db,
        challenge_id,
        f"{_evaluator_dir(challenge_id)}/{name}",
        source.read_bytes(),
    )
    return await _render_evaluator(
        request,
        db,
        course_id,
        challenge_id,
        f"Copied {name} in. Download it to adapt it to this challenge.",
        ok=True,
    )


@app.post("/courses/{course_id}/{challenge_id}/evaluator/reset")
async def post_evaluator_reset(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """Go back to the bundled default, and remove the uploaded script."""
    previous = await query_evaluator_path(db, challenge_id)
    await update_challenge(db, challenge_id, evaluator_path=None)
    await db.commit()
    if previous is not None:
        storage.delete(previous)
    return await _render_evaluator(
        request,
        db,
        course_id,
        challenge_id,
        "Back to the default evaluator: exact match.",
        ok=True,
    )


@app.get("/courses/{course_id}/{challenge_id}/evaluator/download")
async def get_evaluator_download(
    course_id: int,
    challenge_id: int,
    db: DBDep,
    role: ChallengeEditor,
):
    """The current evaluator's source, for staff only.

    Gated even though it is a read: a checker describes the shape of the
    answer, and on a challenge with hidden test cases handing it to a student
    turns "work out the format" into "read the format".
    """
    relative = await query_evaluator_path(db, challenge_id)
    if relative is None:
        path = bundled_path(DEFAULT_EVALUATOR)
    else:
        path = storage.resolve(relative)
    if not path.is_file():
        raise HTTPException(status_code=404)
    # text/plain whatever the extension says, so a script named .html can
    # never be served as a page on this origin.
    return FileResponse(path, media_type="text/plain", filename=path.name)


@app.post("/f/markdown-preview")
async def post_markdown_preview(
    request: Request,
    user: CurrentUser,
    description: Annotated[str, Form()] = "",
):
    """Render the editor's textarea through the same filter the challenge page
    uses, so the preview cannot drift from the real thing."""
    return templates.TemplateResponse(
        request=request,
        name="f-markdown-preview.jinja",
        context={"description": description},
    )


@app.post("/courses/{course_id}/{challenge_id}/testcases")
async def post_testcase(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    input_file: Annotated[UploadFile, File()],
    expected_output_file: Annotated[UploadFile, File()],
    is_sample: Annotated[bool, Form()] = False,
):
    """Create a test case from an uploaded input / expected output pair."""
    try:
        input_name = safe_filename(input_file.filename or "")
        output_name = safe_filename(expected_output_file.filename or "")
    except ValueError as e:
        return await _render_testcases(
            request, db, course_id, challenge_id, str(e), ok=False
        )

    if input_name == output_name:
        return await _render_testcases(
            request, db, course_id, challenge_id,
            "The input and the expected output must be different files.", ok=False,
        )

    for upload in (input_file, expected_output_file):
        if upload.size is not None and upload.size > MAX_TESTCASE_BYTES:
            return await _render_testcases(
                request, db, course_id, challenge_id,
                f"{upload.filename} is larger than "
                f"{MAX_TESTCASE_BYTES // (1024 * 1024)} MB.", ok=False,
            )

    directory = _testcase_dir(challenge_id)
    input_path = f"{directory}/{input_name}"
    output_path = f"{directory}/{output_name}"
    if storage.exists(input_path) or storage.exists(output_path):
        return await _render_testcases(
            request, db, course_id, challenge_id,
            "A file with that name already exists for this challenge.", ok=False,
        )

    # Streamed, not read: test inputs can be several megabytes (DESIGN.md)
    storage.save_stream(input_path, input_file.file)
    storage.save_stream(output_path, expected_output_file.file)

    await insert_testcase(
        db, challenge_id, input_path, output_path, is_sample=is_sample
    )
    await db.commit()
    return await _render_testcases(
        request, db, course_id, challenge_id, f"Added {input_name}.", ok=True
    )


@app.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/delete")
async def post_testcase_delete(
    course_id: int,
    challenge_id: int,
    testcase_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """Delete a test case, but only one nothing has been graded on.

    Once a submission has been scored against a test case, deleting it would
    strand that score: staff retire the case instead, which stops new
    submissions from using it while the history keeps making sense.
    """
    testcase = await query_testcase(db, challenge_id, testcase_id)
    if testcase is None:
        raise HTTPException(status_code=404)

    def refuse(graded: int | None):
        subject = (
            "This test case has already graded submissions"
            if graded is None
            else f"{graded} submission{'' if graded == 1 else 's'} "
                 f"{'was' if graded == 1 else 'were'} graded on this test case"
        )
        return _render_testcases(
            request, db, course_id, challenge_id,
            f"{subject}, so it cannot be deleted. Untick Active to stop using "
            "it for new submissions.",
            ok=False,
        )

    graded = await count_testcase_outcomes(db, testcase_id)
    if graded:
        return await refuse(graded)

    try:
        await delete_testcase(db, testcase_id)
        await db.commit()
    except IntegrityError:
        # The count and the delete are two statements, and a judge may have
        # inserted outcomes in between. The FK catches it; this is not a 500.
        await db.rollback()
        return await refuse(None)

    # Only after the row is gone, so a failed commit cannot leave the database
    # pointing at files that no longer exist.
    storage.delete(testcase.input_path)
    storage.delete(testcase.expected_output_path)
    return await _render_testcases(
        request, db, course_id, challenge_id, "Test case deleted.", ok=True
    )


@app.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/rename")
async def post_testcase_rename(
    course_id: int,
    challenge_id: int,
    testcase_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    kind: Annotated[str, Form()],
    name: Annotated[str, Form()],
):
    """Rename one of a test case's two files, on disk and in the database."""
    if kind not in ("input", "output"):
        raise HTTPException(status_code=404)
    testcase = await query_testcase(db, challenge_id, testcase_id)
    if testcase is None:
        raise HTTPException(status_code=404)

    def fail(message: str):
        return _render_testcases(request, db, course_id, challenge_id, message, ok=False)

    try:
        new_name = safe_filename(name)
    except ValueError as e:
        return await fail(str(e))

    column = "input_path" if kind == "input" else "expected_output_path"
    old_path = getattr(testcase, column)
    new_path = f"{_testcase_dir(challenge_id)}/{new_name}"
    if new_path == old_path:
        return await _render_testcases(request, db, course_id, challenge_id)
    if storage.exists(new_path):
        return await fail(f"{new_name} already exists for this challenge.")

    try:
        storage.rename(old_path, new_path)
    except (OSError, ValueError) as e:
        return await fail(f"Could not rename: {e}")

    await update_testcase(db, testcase_id, **{column: new_path})
    await db.commit()
    return await _render_testcases(
        request, db, course_id, challenge_id, f"Renamed to {new_name}.", ok=True
    )


@app.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/sample")
async def post_testcase_sample(
    course_id: int,
    challenge_id: int,
    testcase_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    is_sample: Annotated[bool, Form()] = False,
):
    """Publish or hide a test case. A hidden one stops being downloadable."""
    if await query_testcase(db, challenge_id, testcase_id) is None:
        raise HTTPException(status_code=404)
    await update_testcase(db, testcase_id, is_sample=is_sample)
    await db.commit()
    return await _render_testcases(
        request, db, course_id, challenge_id,
        "Now a public sample." if is_sample else "No longer public.", ok=True,
    )


@app.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/active")
async def post_testcase_active(
    course_id: int,
    challenge_id: int,
    testcase_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    is_active: Annotated[bool, Form()] = False,
):
    """Retire or reinstate a test case.

    Retiring is the non-destructive alternative to deleting: the row, the files
    and every outcome scored against it stay, but the judge stops running it and
    it is no longer downloadable as a sample.
    """
    if await query_testcase(db, challenge_id, testcase_id) is None:
        raise HTTPException(status_code=404)
    await update_testcase(db, testcase_id, is_active=is_active)
    await db.commit()
    return await _render_testcases(
        request, db, course_id, challenge_id,
        "Active again — new submissions are graded on it."
        if is_active
        else "Retired — new submissions will skip it.",
        ok=True,
    )


@app.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/move")
async def post_testcase_move(
    course_id: int,
    challenge_id: int,
    testcase_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    direction: Annotated[str, Form()],
):
    """Swap a test case with its neighbour. Buttons rather than drag-and-drop:
    the repo has no JS build step, and this is two integers."""
    if direction not in ("up", "down"):
        raise HTTPException(status_code=404)

    ordered = await query_challenge_testcases(db, challenge_id)
    index = next(
        (i for i, tc in enumerate(ordered) if tc.id == testcase_id), None
    )
    if index is None:
        raise HTTPException(status_code=404)

    neighbour = index - 1 if direction == "up" else index + 1
    if not 0 <= neighbour < len(ordered):
        # Already at the end; nothing to do rather than an error
        return await _render_testcases(request, db, course_id, challenge_id)

    await swap_testcase_order(db, testcase_id, ordered[neighbour].id)
    await db.commit()
    return await _render_testcases(request, db, course_id, challenge_id)


@app.get("/users/{user_id}/avatar")
async def user_avatar(user_id: int, db: DBDep, user: CurrentUser):
    row = (
        await db.execute(
            select(User.c.image, User.c.image_mime).where(User.c.id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404)
    return Response(content=row.image, media_type=row.image_mime)

