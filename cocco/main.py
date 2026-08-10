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
from cocco.evaluator import EqualityEvaluator
from cocco.judge import load_languages
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
    MAX_LINKS_PER_ADDRESS,
    MAX_LINKS_PER_CLIENT,
    RATE_WINDOW,
    TOKEN_TTL,
    build_magic_link,
    generate_token,
    get_or_create_user,
    hash_token,
    is_expired,
    load_config,
    normalize_email,
    safe_next_path,
    send_magic_link,
)
from fastapi import (
    BackgroundTasks,
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
from collections import defaultdict, deque
from datetime import datetime
import logging
import os

from cocco.repository import (
    consume_login_token,
    count_recent_login_tokens,
    count_running_submissions,
    count_testcase_outcomes,
    delete_expired_login_tokens,
    delete_login_tokens_for_email,
    delete_testcase,
    insert_enrollment,
    insert_login_token,
    insert_submission,
    insert_testcase,
    insert_user,
    query_challenge,
    query_challenge_leaderboard,
    query_challenge_ranking,
    query_challenge_standings,
    query_challenge_testcases,
    query_course_info,
    query_courses,
    query_sample_testcase,
    query_submission,
    query_submission_outcomes,
    query_testcase,
    query_user,
    query_user_role,
    query_user_submissions,
    regrade_challenge,
    swap_testcase_order,
    update_challenge,
    update_testcase,
)


logger = logging.getLogger(__name__)

# SMTP, the session secret and the public base URL. A missing file is fine: the
# defaults log the magic link instead of mailing it.
CONFIG = load_config(Path(os.environ.get("COCCO_CONFIG", "cocco.toml")))

# Development shortcut: the login form takes an address at face value, with no
# token and no mail. `post_login` is the only place that branches on it.
DUMMY_AUTH = os.environ.get("COCCO_DUMMY_AUTH") == "1"

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
    # Not "strict": a magic link is a top-level navigation out of a mail
    # client, and under "strict" the cookie just set by /auth/verify would not
    # be sent on the redirect that follows it.
    same_site="lax",
)

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
# needs no language picker. `languages.toml` is the same file the judge loads.
LANGUAGES = load_languages(Path("languages.toml"))
EXTENSION_TO_LANGUAGE = {
    ext.lower(): name
    for name, language in LANGUAGES.items()
    for ext in language.extensions
}

MAX_UPLOAD_BYTES = 1 << 20

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


# The development cast. With COCCO_DUMMY_AUTH=1 any of these addresses gets you
# straight in, so the staff view and the student view are both one login away.
DUMMY_STAFF = ["alice@unipd.it", "matteo.ceccarello@unipd.it"]
DUMMY_STUDENTS = ["bob@unipd.it", "charlie@unipd.it"]


async def _populate_dummy_data(conn: AsyncConnection):
    import textwrap

    user_ids = {
        email: await _seed_user(conn, email, email.split("@", 1)[0])
        for email in DUMMY_STAFF + DUMMY_STUDENTS
    }
    # The student whose attempts the seeded submission history belongs to.
    student_id = user_ids["bob@unipd.it"]

    evaluator = EqualityEvaluator()
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
        # Alice and Matteo are course staff on everything; Bob and Charlie are
        # students. `role` is per course, so this is two rows per person.
        for email in DUMMY_STAFF:
            await insert_enrollment(
                conn, user_ids[email], course_id, UserRole.admin
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
                        # A CPython interpreter alone needs well over a few MB,
                        # so these have to be realistic or every run is killed.
                        max_memory_bytes=256 << 20,
                        max_time_s=5.0,
                        max_threads=1,
                        evaluator=evaluator,
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
    """
    user_id = request.session.get("user_id")
    if user_id is not None:
        row = await query_user(db, user_id)
        if row is not None:
            return schema.User.model_validate(row)
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


########################################################################
# authentication
#
# A visitor proves an address by following a one-time link mailed to it, and
# the account is created the first time such a link is followed. None of these
# routes depends on `CurrentUser`: they are the way in.

# The per-client half of the rate limit, a sliding window of request times. It
# is per process, so `fastapi dev` resets it on every reload and two workers do
# not agree - fine for a self-hosted judge, and it costs no new dependency.
_LINK_REQUESTS: dict[str, deque[datetime]] = defaultdict(deque)


def _client_budget_ok(request: Request, now: datetime) -> bool:
    key = request.client.host if request.client else "unknown"
    window = _LINK_REQUESTS[key]
    while window and window[0] < now - RATE_WINDOW:
        window.popleft()
    if len(window) >= MAX_LINKS_PER_CLIENT:
        return False
    window.append(now)
    return True


def _base_url(request: Request) -> str:
    """The absolute origin the emailed link should point at. Configured
    explicitly when there is a proxy in front, since only the deployment knows
    the public name."""
    if CONFIG.base_url:
        return CONFIG.base_url
    return str(request.base_url).rstrip("/")


def _login_page(
    request: Request,
    *,
    next_path: str = "/",
    error: str | None = None,
    sent_to: str | None = None,
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
            "sent_to": sent_to,
            "ttl_minutes": int(TOKEN_TTL.total_seconds() // 60),
            "dummy_auth": DUMMY_AUTH,
        },
    )


def _sign_in(request: Request, user: schema.User, next_path: str):
    # Never carry a prior session over into a new identity.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(next_path, status_code=303)


@app.get("/login")
async def get_login(request: Request, next: str = "/"):
    return _login_page(request, next_path=safe_next_path(next))


@app.post("/login")
async def post_login(
    request: Request,
    db: DBDep,
    background: BackgroundTasks,
    email: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    """Send a magic link - or, under COCCO_DUMMY_AUTH, just sign the visitor in.

    The response is the same whether or not the address is known and whether or
    not a link was actually sent, so neither a rate limit nor the existence of
    an account is observable from here. There is no account to enumerate
    anyway: a first login creates one.
    """
    next_path = safe_next_path(next)
    try:
        address = normalize_email(email)
    except ValueError:
        return _login_page(
            request,
            next_path=next_path,
            error="That does not look like an email address.",
            status_code=400,
        )

    if DUMMY_AUTH:
        # The development shortcut, and the only branch that skips confirming
        # the address: `just dev` needs no mail server.
        user = await get_or_create_user(db, address)
        await db.commit()
        return _sign_in(request, user, next_path)

    now = datetime.now()
    await delete_expired_login_tokens(db, now)
    recent = await count_recent_login_tokens(db, address, now - RATE_WINDOW)
    if recent < MAX_LINKS_PER_ADDRESS and _client_budget_ok(request, now):
        token = generate_token()
        await insert_login_token(
            db, hash_token(token), address, now, now + TOKEN_TTL
        )
        # The token must be durable before the mail carrying it can arrive.
        await db.commit()
        link = build_magic_link(_base_url(request), token)
        # A sync callable, so Starlette runs blocking smtplib in the threadpool.
        background.add_task(send_magic_link, CONFIG.smtp, address, link)
    else:
        await db.commit()  # the expiry sweep above
        logger.warning("rate-limited a magic-link request for %s", address)

    request.session["login_next"] = next_path
    return _login_page(request, next_path=next_path, sent_to=address)


@app.get("/auth/verify")
async def get_auth_verify(request: Request, db: DBDep, token: str = ""):
    now = datetime.now()
    row = await consume_login_token(db, hash_token(token), now)
    if row is None or is_expired(row.expires_at, now):
        # Unknown, already spent, or expired - all the same message, and the
        # burn stands either way.
        await db.commit()
        return _login_page(
            request,
            error="That link is no longer valid. Ask for a new one.",
            status_code=400,
        )

    user = await get_or_create_user(db, row.email)
    # An older link that was forwarded on is now dead.
    await delete_login_tokens_for_email(db, row.email)
    await db.commit()

    # The link may well be opened in another browser, where the session that
    # asked for it does not exist; then there is no destination but the root.
    next_path = safe_next_path(request.session.get("login_next"))
    return _sign_in(request, user, next_path)


@app.post("/logout")
async def post_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def require_challenge_editor(
    challenge_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """Authorize an edit of `challenge_id`, or raise 403.

    Hiding the Edit button in the template is a UI affordance, not access
    control: every route that mutates a challenge depends on this.
    """
    role = await query_user_role(db, user_id=user.id, challenge_id=challenge_id)
    if role is None or role not in EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Not course staff")
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
        raise HTTPException(status_code=403, detail="Not your submission")
    return is_staff


# Fields of an outcome that are free text produced by the run rather than by the
# judge's own bookkeeping. `stdout`/`stderr` are the submitted program's output,
# and `message` can be the evaluator's diff - i.e. the expected output itself
# (see DiffEvaluator). On a hidden test case all three would hand a student the
# test data, so they are only shown for samples, or to course staff.
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
async def index(request: Request, db: DBDep, user: CurrentUser):
    courses = await query_courses(db, user_id=user.id)
    return templates.TemplateResponse(
        request=request,
        name="index-logged-in.jinja",
        context={
            "courses": courses,
            "title": "COCCO: Coding Challenges Companion",
            "user": user,
        },
    )

@app.get("/courses/{course_id}")
async def get_course(course_id: int, request: Request, db: DBDep, user: CurrentUser):
    from icecream import ic
    course = await query_course_info(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    ic(course)
    return templates.TemplateResponse(
        request=request,
        name="course.jinja",
        context={
            "course": course,
            "title": course["title"],
            "user": user,
        },
    )

@app.get("/courses/{course_id}/{challenge_id}")
async def get_challenge(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    as_: Annotated[str | None, Query(alias="as")] = None,
):
    """The challenge, in whichever of its two views fits the viewer.

    Course staff get `challenge-staff.jinja`: the statement plus the whole
    class's standings, and no submit form. `?as=student` gives them the page a
    student sees, which is the only way to check how the statement reads.
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
    solution: Annotated[UploadFile, File()],
):
    """Enqueue an uploaded solution. The judge process picks it up from there.

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
    challenge_id: int, request: Request, db: DBDep, user: CurrentUser
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
    challenge_id: int, request: Request, db: DBDep, user: CurrentUser
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
    challenge_id: int, testcase_id: int, kind: str, db: DBDep, user: CurrentUser
):
    """Serve the input or expected output of a *public* test case.

    `query_sample_testcase` returns nothing for hidden test cases, so those
    404 rather than leaking the judge's data. The viewer must additionally be
    enrolled in the course: a sample belongs to the class it was set for, and
    an id is easy to guess.
    """
    if kind not in ("input", "output"):
        raise HTTPException(status_code=404)
    if await query_user_role(db, user_id=user.id, challenge_id=challenge_id) is None:
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


@app.get("/f/students/{challenge_id}/{student_id}")
async def get_fragment_student_submissions(
    challenge_id: int,
    student_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """One student's attempts, for the staff page. Same query and same fragment
    as the student's own history - only the user id differs."""
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
# The challenge editor. Every route here depends on ChallengeEditor, which
# 403s anyone who is not staff on the challenge's course.


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
            "user": user,
            "course_id": course_id,
            "challenge_id": challenge_id,
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

    if max_time_s <= 0 or max_memory_mb <= 0 or max_threads < 1:
        return alert("Limits must be positive.")

    try:
        criterion = LeaderboardRanking(ranking)
    except ValueError:
        return alert("Unknown leaderboard ranking.")

    await update_challenge(
        db,
        challenge_id,
        description=description,
        max_time_s=max_time_s,
        max_memory_bytes=int(max_memory_mb * 1024 * 1024),
        max_threads=max_threads,
        ranking=criterion,
    )
    await db.commit()
    return alert("Saved.", ok=True)


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

