from typing import Annotated
import markdown as md
from markupsafe import Markup
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound
from sqlalchemy import insert, select
from cocco.avatar import MAX_AVATAR_BYTES, generate_identicon, sniff_image_mime
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
    normalize_email,
    safe_next_path,
)
from cocco.config import config_path, load_config
from cocco.tokens import hash_token, mint_token, parse_bearer
from cocco import transfer
from cocco.transfer import (
    EVALUATOR_DIR,
    MANIFEST_NAME,
    STATEMENT_NAME,
    TESTS_DIR,
    ExistingTestCase,
    Manifest,
    ManifestError,
    ManifestTestCase,
    dump_manifest,
    parse_manifest,
    plan_testcases,
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
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware
import hmac
import io
import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

from cocco.repository import (
    block_user,
    challenge_in_course,
    count_running_submissions,
    count_testcase_outcomes,
    course_exists,
    delete_api_token,
    delete_challenge,
    delete_course,
    delete_enrollment,
    delete_testcase,
    insert_administrators,
    insert_api_token,
    insert_challenge,
    insert_course,
    insert_enrollment,
    insert_self_enrollment,
    insert_submission,
    insert_testcase,
    insert_user,
    is_administrator,
    lock_challenge_submissions,
    lock_course,
    lock_course_submissions,
    query_api_token_user,
    query_api_tokens,
    query_challenge,
    query_challenge_config,
    query_challenge_file_paths,
    query_challenge_leaderboard,
    query_challenge_ranking,
    query_challenge_standings,
    query_challenge_testcases,
    query_course,
    query_course_archived_at,
    query_course_challenges,
    query_course_info,
    query_course_people,
    query_course_role,
    query_courses,
    query_editable_challenges,
    query_effective_archived_at,
    query_enrollment_key,
    query_evaluator_path,
    query_ordered_challenge_ids,
    query_sample_testcase,
    query_submission,
    query_submission_outcomes,
    query_testcase,
    query_user,
    query_user_by_email,
    query_user_profile,
    query_user_role,
    query_user_submissions,
    query_users,
    regrade_challenge,
    reorder_challenges,
    set_challenge_archived,
    set_course_archived,
    set_enrollment_role,
    swap_testcase_order,
    touch_api_token,
    unblock_user,
    update_challenge,
    update_course,
    update_testcase,
    update_user,
)


logger = logging.getLogger(__name__)

# Everything the app is configured with: the database, the storage root, the
# allowed domains, the session cookie and the development shortcuts. A missing
# file is fine - the app boots on the defaults, and the login page says that
# Google sign-in is unconfigured.
CONFIG = load_config(config_path())

# The development cast, and the addresses `post_auth_dummy` is there to be
# typed. Matteo and Alice both teach every seeded course; what separates them is
# that Matteo is a site administrator - a row in `administrator`, not a role on
# any enrollment - so he may create courses and is staff on courses nobody
# enrolled him on. Bob and Charlie are students.
DUMMY_ADMIN = "matteo.ceccarello@unipd.it"
DUMMY_TEACHERS = ["alice@unipd.it"]
DUMMY_STAFF = [DUMMY_ADMIN, *DUMMY_TEACHERS]
DUMMY_STUDENTS = ["bob@unipd.it", "charlie@unipd.it"]

# Development shortcut: no Google and no credentials. `/login` grows a box, and
# whatever address is typed into it is signed in - so it is `post_auth_dummy`
# that branches on this, and `get_user` no longer does. On anything public it is
# equivalent to publishing every account's password.
#
# Derived from the config once, here, and thereafter *this name* is the app's
# switch, read rather than `CONFIG.dev`: it is the seam the route tests flip,
# and a frozen dataclass field has no equivalent. Do not "tidy" it away.
DUMMY_AUTH = CONFIG.dev.dummy_auth

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

# Static files. Resolved against the package rather than the working directory,
# the rule `load_languages` already follows for `languages.toml` - and inside
# `cocco/` rather than beside it, so an installed `cocco-web` has them too.
STATIC_DIR = Path(__file__).resolve().parent / "static"

# The templates, by the same rule. Both of these used to be bare relative
# strings handed to Starlette and Jinja, which meant the app ran from the
# repository root and nowhere else - and, worse for `static`, meant the mount
# and `static_url` below could name two different directories.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class VersionedStaticFiles(StaticFiles):
    """`StaticFiles` with a `Cache-Control`, chosen by whether the URL carries a
    `?v=`.

    Starlette sets a `last-modified` and an `etag` and no freshness directive at
    all, so a browser falls back to heuristic caching - which for a file that
    was created recently is worth nothing. Every reload therefore revalidated
    the navbar mark, and the navbar painted an empty box until the 304 came
    back. That is the flicker.

    A year of `immutable` is only safe on a URL that changes when the file does,
    which is what `static_url` stamps on; a URL typed by hand has no version to
    change and so gets `no-cache` - it may be held in cache, but never used
    without asking. The two halves have to stay together: the header is the
    point of the version, and the version is what makes the header safe."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # The `v` *key*, not the substring "v=", which `?nv=1` also contains.
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        # Set after `super()`, so this covers the 304 too: `NotModifiedResponse`
        # keeps `cache-control` among the few headers it carries over.
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "no-cache"
        )
        return response


app.mount("/static", VersionedStaticFiles(directory=STATIC_DIR), name="static")

# The database. The judge builds a *sync* engine from this same URL, which is
# why the driver in it has to be one with both faces - see DEFAULT_DATABASE_URL.
engine = create_async_engine(CONFIG.database.url)

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Test case inputs and expected outputs live on the filesystem, not in the
# database (see DESIGN.md). The judge resolves the same paths against the same
# root, and the two agree because they read it out of the same file.
storage = Storage(CONFIG.storage.root)

# Uploads are mapped to a language by their extension, so the challenge page
# needs no language picker. `languages.toml` is the same file the judge loads -
# literally, since `load_languages` resolves it against the checkout rather
# than the working directory - and so is the mapping: an evaluator script is
# recognised by exactly the rule a submitted solution is.
LANGUAGES = load_languages()
EXTENSION_TO_LANGUAGE = extension_to_language(LANGUAGES)

MAX_UPLOAD_BYTES = 1 << 20

# An evaluator is a source file, not test data, so it lives under the same
# ceiling as a submitted solution rather than the test case one.
MAX_EVALUATOR_BYTES = MAX_UPLOAD_BYTES

# Test case inputs are routinely far larger than a solution file, so they get
# their own, much higher ceiling - and are streamed to disk, never buffered.
MAX_TESTCASE_BYTES = 32 << 20

# A pushed challenge carries every test case at once, so its ceiling is a
# multiple of one test case's rather than equal to it. Checked twice, on the
# compressed bytes as they arrive and on the *uncompressed* total declared in
# the zip's directory before a single member is read: a cap on compressed size
# alone is not a cap at all, which is what a zip bomb is.
MAX_TRANSFER_BYTES = 256 << 20

# Course staff. `role` lives on `enrollment`, so these are per-course, not
# site-wide. Everyone else - i.e. students and the unenrolled - is read-only.
# A site administrator is staff on every course without appearing here: that is
# resolved in `effective_course_role`, not by widening this set.
EDITOR_ROLES = {UserRole.teacher, UserRole.collaborator}
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

# Every role there is, in the order the people page offers them - strings,
# because that is what `row_to_json` gives the template to compare against.
# A global for the same reason: the fragment that builds the selector is served
# from four routes, and the list of roles is not any of their business.
USER_ROLE_NAMES = [role.value for role in UserRole]
templates.env.globals["USER_ROLE_NAMES"] = USER_ROLE_NAMES


def static_url(path: str) -> str:
    """`/static/<path>` with the file's mtime as `?v=`, which is what opts it
    into `VersionedStaticFiles`' year of `immutable`.

    The `stat` happens per render rather than once at import, and that is
    load-bearing: `npm run watch:css` rewrites `cocco/static/styles.css` without
    touching a `.py`, so a version fixed at import would pin a year-long
    `max-age` to a file that has since changed, with nothing left to invalidate
    it. Three files and one `stat` each is not worth caching around.

    A missing file falls back to the bare path - `cocco/static/styles.css` is
    a build artifact and is simply absent until the first `npm run build:css`.

    The URL is built by hand rather than through `url_for`, matching what the
    templates already did, and so ignores `root_path`. It takes literals from
    templates and never anything a user typed."""
    try:
        version = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version:x}"


templates.env.globals["static_url"] = static_url


async def create_db_and_tables(dummy=False):
    async with engine.begin() as conn:
        if dummy:
            await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
        if dummy:
            await _populate_dummy_data(conn)
        # The configured administrators, replayed on every boot: this is how a
        # brand new deployment gets its first one, since there is no
        # administrator to appoint another. It only ever adds - see
        # `insert_administrators` - so a database that has moved on is left
        # alone, and deleting a line from `cocco.toml` demotes nobody.
        await insert_administrators(conn, CONFIG.auth.administrators)
        if DUMMY_AUTH:
            # `cocco.dev.toml` deliberately names no administrator - a file
            # every checkout shares should not make one person the
            # administrator of all of them - and the promise of dummy auth is
            # that the checkout is usable as it stands. Only DUMMY_ADMIN, so
            # that typing `alice@unipd.it` into the sign-in box still shows what
            # plain course staff see rather than quietly promoting her.
            await insert_administrators(conn, [DUMMY_ADMIN])


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
    last_challenge_id: int | None = None
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
        # Matteo and Alice both teach; Bob and Charlie are students. `role` is
        # per course, so this is one row per person per course. What makes
        # Matteo an administrator is not written here but in `administrator`,
        # by the bootstrap in `create_db_and_tables`.
        await insert_enrollment(
            conn, user_ids[DUMMY_ADMIN], course_id, UserRole.teacher
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
            last_challenge_id = challenge_id

    assert first_challenge_id is not None

    # One archived challenge inside a live course, and one archived course, so
    # both halves of archiving are visible in a fresh checkout without anybody
    # having to click. Alice is deliberately *not* enrolled on the archived
    # course: `[dev] dummy_user = "alice@unipd.it"` is then the view in which
    # an archived course is simply not listed, while Matteo - an administrator,
    # and so the only person who can unarchive one - finds it under "Other
    # courses", and Bob and Charlie see it in "Your courses", greyed and
    # read-only.
    from datetime import datetime

    assert last_challenge_id is not None
    await set_challenge_archived(conn, last_challenge_id, True)
    archived_course_id = (
        await conn.execute(
            insert(Course)
            .values(
                title="Algorithms 101 (2024)",
                description=(
                    "Last year's edition, kept for the record. Enrolled "
                    "students can still read it and look back over their "
                    "submissions."
                ),
                archived_at=datetime.now(),
            )
            .returning(Course.c.id)
        )
    ).scalar_one()
    await insert_enrollment(
        conn, user_ids[DUMMY_ADMIN], archived_course_id, UserRole.teacher
    )
    for email in DUMMY_STUDENTS:
        await insert_enrollment(
            conn, user_ids[email], archived_course_id, UserRole.student
        )
    await conn.execute(
        insert(Challenge).values(
            title="Shortest Route Planner (2024)",
            description=shortest_route,
            course_id=archived_course_id,
            position=0,
            max_memory_bytes=256 << 20,
            max_time_s=5.0,
            max_threads=1,
            evaluator_path=None,
        )
    )

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
        classmate_id = await _seed_user(conn, f"{nickname}@studenti.unipd.it", nickname)
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


def _refuse_if_blocked(user: schema.User) -> None:
    """The site-wide gate, asked of the row `get_user` has already read.

    Free, which is why the block lives on `user` rather than in a table of its
    own, and asked again on every request, so it takes effect on the blocked
    person's very next click - exactly as a deleted account does.
    """
    if user.blocked_at is not None:
        raise Blocked(user.blocked_reason)


async def get_user(request: Request, db: DBDep) -> schema.User:
    """The signed-in user, or `NotAuthenticated` - or `Blocked`.

    The cookie holds nothing but the user id, so a renamed - or deleted, or
    blocked - account takes effect on the very next request.

    The user is also stashed on `request.state`, for the one caller that cannot
    ask for a dependency: `handle_forbidden` renders a whole page, navbar
    included, and this has always run by the time an authorization dependency
    can refuse. The block is checked *before* the stash, deliberately - see
    `handle_blocked`, which wants no viewer at all.

    There is **one branch**, and that is the point of `post_auth_dummy` being a
    sign-in rather than a bypass: development used to resolve an address out of
    the configuration on every request, which made this function two functions
    and meant every rule here - the block, the stale session, the redirect - had
    to be written twice and could be got wrong in the half nobody browses in.
    """
    user_id = request.session.get("user_id")
    if user_id is not None:
        row = await query_user(db, user_id)
        if row is not None:
            user = schema.User.model_validate(row)
            _refuse_if_blocked(user)
            request.state.user = user
            return user
        # A session pointing at nobody: in development, typically the other
        # side of a `[dev] reset_db`.
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


class Blocked(Forbidden):
    """An account an administrator has shut out of the site.

    A subclass of `Forbidden` rather than a fresh `HTTPException`: it is still
    "a 403 that renders as a page", and Starlette resolves a handler along the
    exception's MRO, so deleting the handler below would degrade this to
    `403.jinja` rather than to raw JSON.

    Its own class all the same, because it is not a refusal about the URL that
    was asked for - nothing on the site is open to them - and the page has to
    say so and carry the sentence somebody wrote for them.
    """

    DEFAULT_REASON = "No reason was recorded."

    def __init__(self, reason: str | None):
        self.reason = reason or self.DEFAULT_REASON
        super().__init__(f"This account is blocked. {self.reason}")


@app.exception_handler(Blocked)
async def handle_blocked(request: Request, exc: Blocked):
    """The refusal a blocked account gets, on every request it makes.

    Rendered with no viewer, and that is not modesty: the navbar's avatar is
    `/users/{id}/avatar`, itself behind `CurrentUser`, so it would 403 into a
    broken image on the very page explaining the block. `get_user` raises
    before it stashes `request.state.user`, which is what makes that possible.

    The session is left alone. Clearing it would turn every later request into
    `NotAuthenticated` and send them to a login page that explains nothing,
    while this page explains everything - and the cookie holds only an id,
    which is re-checked here on every request anyway. `get_user` still clears a
    session pointing at *nobody*: that one is stale, this one is not.

    Polled fragments are left to freeze, as they do for a student unenrolled
    mid-session: htmx does not swap the body of an error response. Deliberately
    not the `HX-Redirect` `handle_not_authenticated` sends, which would conflate
    being blocked with being signed out and land them on the one page that
    cannot tell them what happened.
    """
    return templates.TemplateResponse(
        request=request,
        name="blocked.jinja",
        status_code=403,
        context={
            "title": "Account blocked",
            "reason": exc.reason,
            "user": None,
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
            # Draws the development sign-in box. See `post_auth_dummy`.
            "dummy_auth": DUMMY_AUTH,
            # The seeded cast, so the box can say who there is to be. Passed
            # from here rather than spelled in the template, which is what stops
            # it drifting from the addresses `_populate_dummy_data` writes.
            "dummy_admin": DUMMY_ADMIN,
            "dummy_teachers": DUMMY_TEACHERS,
            "dummy_students": DUMMY_STUDENTS,
        },
    )


def _sign_in(request: Request, user: schema.User, next_path: str):
    # Never carry a prior session over into a new identity - and this is also
    # what drops the OAuth state Authlib left behind, now spent.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(next_path, status_code=303)


async def _complete_sign_in(
    request: Request, db: AsyncConnection, email: str, next_path: str
):
    """Everything between a proved address and a session, for both of the ways
    an address is proved.

    Google's callback proves one by verifying an ID token; `post_auth_dummy`
    "proves" one by being development. What has to happen next is the same three
    rules either way - the domain allowlist, the account, the blacklist - and
    they are written here once rather than restated at each end, the way
    `_challenge_fields` is shared by the three forms that build a challenge. A
    rule the browser enforced and the box did not would be a difference nobody
    could explain, and the box is the configuration this all gets developed in.

    The address must already be through `normalize_email`: canonicalizing it is
    each caller's, because the sentence for an unusable one differs - Google
    returning no address is not the same event as an empty text box.
    """
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

    if user.blocked_at is not None:
        # Refused here as well as in `get_user`, and after the domain check so
        # that the permanent reason keeps precedence. Without this the sign-in
        # succeeds and dies on the first click, on a page they did not ask for;
        # this way the sentence arrives on the page they are already looking at.
        # `get_or_create_user` created nothing above - a blocked account exists.
        logger.info("refused a sign-in from a blocked account: %s", email)
        return _login_page(
            request,
            next_path=next_path,
            error=f"This account is blocked. {user.blocked_reason or ''}".strip(),
            status_code=403,
        )

    return _sign_in(request, user, next_path)


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
    return _login_page(request, next_path=safe_next_path(next))


@app.post("/auth/dummy")
async def post_auth_dummy(
    request: Request,
    db: DBDep,
    email: str = Form(""),
    next: str = Form("/"),
):
    """Sign in as whoever was typed. Development only, and hard-refused
    otherwise.

    This is the whole of `[dev] dummy_auth`. It replaces a shortcut that signed
    every request in as one address named in the configuration file, which meant
    that seeing what a student sees was an edit and a restart - on an app whose
    every permission is per course, so hopping between the seeded cast is the
    single most common thing one does while developing it.

    A **404** rather than a 403 when the switch is off: this route signs the
    caller in as anybody who can be named, so a deployment should not so much as
    admit that the path exists. Beyond that its defence is the app's only one,
    `same_site="lax"` - a cross-site POST carries no session, and there is
    nothing here worth a token that every other mutating route does without.

    Both fields are **defaulted** rather than required, for the reason
    `post_administrator`'s are: a missing one is a 422 with no page on it, and
    there is a person in front of this box who deserves a sentence.
    """
    if not DUMMY_AUTH:
        raise HTTPException(status_code=404)

    next_path = safe_next_path(next)
    try:
        address = normalize_email(email)
    except ValueError as err:
        return _login_page(
            request, next_path=next_path, error=str(err), status_code=400
        )
    logger.warning("dummy sign-in as %s", address)
    return await _complete_sign_in(request, db, address, next_path)


@app.get("/auth/google")
async def get_auth_google(request: Request, next: str = "/"):
    """Hand off to Google.

    Authlib puts the `state`, the `nonce` and the PKCE verifier in the session
    and returns the redirect to the consent screen.
    """
    next_path = safe_next_path(next)
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

    # The domain, the account and the blacklist, shared with the sign-in box.
    return await _complete_sign_in(request, db, email, next_path)


@app.post("/logout")
async def post_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def require_admin(db: DBDep, user: CurrentUser) -> schema.User:
    """Authorize a site administrator, or raise 403.

    The only permission in the app that names no course - which is exactly why
    it is not a `UserRole`: it is a row in `administrator`, keyed on the
    address, put there by `cocco.toml`, by the admin page or by hand. It guards
    creating a course, where the course does not exist yet to have a role on,
    and the admin page, which is about the site rather than about any course.

    The sentence names the permission rather than either of those two, so a
    third one does not find the wrong refusal waiting for it.

    Returns the user, so a route that needs the creator does not ask twice.
    Hiding the button on the home page is an affordance, not this check.
    """
    if not await is_administrator(db, user.email):
        raise Forbidden("Only a site administrator can do that.")
    return user


Admin = Annotated[schema.User, Depends(require_admin)]


async def effective_course_role(
    db: AsyncConnection, user: schema.User, course_id: int
) -> UserRole | None:
    """The role every check on a course is made against: the enrollment's, or
    `teacher` for a site administrator, who is course staff everywhere.

    Substituting `teacher` is not a fiction bolted on here - it is already this
    app's spelling of full staff on a course, and the very row `post_course_new`
    writes for the administrator who creates one. Saying it in one place is what
    keeps every `role in EDITOR_ROLES` downstream, in Python and in the
    templates, true without being touched.

    The administrator lookup only runs when the enrollment did not already
    answer the question, so course staff pay nothing for it.
    """
    role = await query_course_role(db, user_id=user.id, course_id=course_id)
    if role in EDITOR_ROLES:
        return role
    if await is_administrator(db, user.email):
        return UserRole.teacher
    return role


async def effective_challenge_role(
    db: AsyncConnection, user: schema.User, challenge_id: int
) -> UserRole | None:
    """`effective_course_role` for the routes whose URL carries only a
    challenge, resolving the course through it exactly as `query_user_role`
    does."""
    role = await query_user_role(db, user_id=user.id, challenge_id=challenge_id)
    if role in EDITOR_ROLES:
        return role
    if await is_administrator(db, user.email):
        return UserRole.teacher
    return role


async def require_course_editor(
    course_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """Authorize course staff on `course_id`, or raise 403.

    Keyed on the course rather than on a challenge, so it also answers the one
    question a challenge cannot: whether this person may create one here.
    Hiding a button in the template is a UI affordance, not access control.
    """
    role = await effective_course_role(db, user, course_id)
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
    the same `effective_course_role` `require_course_editor` uses: the role is
    per course, so the course in the path is the key - and a site administrator
    is staff on every course, so they are inside every gate.

    What it does *not* guard is the course page itself - `get_course` shows a
    non-member the title, the description and the way in - nor the home page,
    which has to go on listing every course or nobody could find one to enroll
    on, nor `post_course_enroll`, which is that way in.
    """
    role = await effective_course_role(db, user, course_id)
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

    A site administrator passes this without an enrollment, and so also passes
    it for a challenge id that does not exist at all - the route's own query
    then finds nothing, which is the empty fragment it would have rendered for
    a challenge whose contents are gone anyway.
    """
    role = await effective_challenge_role(db, user, challenge_id)
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


########################################################################
# The API's own two dependencies.
#
# Everything above answers a browser: `NotAuthenticated` becomes a 303 to
# `/login` or an `HX-Redirect`, and `Forbidden` renders `403.jinja` through a
# template. Both are nonsense to a program - a CLI following that 303 would be
# handed a login page with a 200 on it - so the API refuses with real status
# codes and a JSON body, which is what FastAPI's own handler does for a plain
# `HTTPException`.


class ApiNotAuthenticated(HTTPException):
    """A 401 for a caller that has no browser to send anywhere.

    Deliberately not `NotAuthenticated`, whose handler is the whole reason that
    class is not an `HTTPException` in the first place: it has to decide between
    a redirect and a header, and neither is an answer here.
    """

    def __init__(self, detail: str):
        super().__init__(
            status_code=401,
            detail=detail,
            # The scheme, so a generic HTTP client knows what it was supposed
            # to send rather than only that it was turned away.
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_api_user(request: Request, db: DBDep) -> schema.User:
    """Who is calling the API: a bearer token, or the browser's own session.

    Two ways in, in this order.

    `Authorization: Bearer`, which is what a command line actually uses. The
    token is never stored, so the lookup is by its hash - which means an unknown
    token and a revoked one are the same miss, as they should be.

    Then the session cookie, so a signed-in browser can follow a plain Download
    link to an export URL. That fallback is safe on the mutating half for the
    same reason every other POST in this app is: `same_site="lax"` means the
    session cookie is not sent on a cross-site POST at all, and that is the
    whole CSRF defence here as everywhere else.

    There is deliberately no `DUMMY_AUTH` branch, and there used to be: it came
    first and signed every API call in as the configured address, which meant a
    bearer token presented to a `just dev` server was *ignored* - the one
    configuration where the header this function is mostly about did nothing.
    The cost is that `cocco pull` against `just dev` now needs a token like
    anywhere else: mint one on the profile page and put it in `COCCO_TOKEN`.
    """
    presented = parse_bearer(request.headers.get("Authorization"))
    if presented is not None:
        token_hash = hash_token(presented)
        row = await query_api_token_user(db, token_hash)
        if row is None:
            raise ApiNotAuthenticated("Unknown or revoked API token.")
        user = schema.User.model_validate(row)
        _refuse_if_blocked(user)
        # One UPDATE per accepted call. It makes a GET write, which is worth
        # saying out loud, and it is the only thing that can tell a token's
        # owner it has gone stale before they revoke it.
        await touch_api_token(db, token_hash)
        await db.commit()
        request.state.user = user
        return user

    user_id = request.session.get("user_id")
    if user_id is not None:
        row = await query_user(db, user_id)
        if row is not None:
            user = schema.User.model_validate(row)
            _refuse_if_blocked(user)
            request.state.user = user
            return user

    raise ApiNotAuthenticated(
        "No API token. Mint one on your profile page and send it as "
        "`Authorization: Bearer <token>`."
    )


ApiUser = Annotated[schema.User, Depends(get_api_user)]


async def require_api_challenge_editor(
    challenge_id: int, db: DBDep, user: ApiUser
) -> UserRole:
    """Authorize a pull or a push of `challenge_id`, or raise 403.

    Keyed on the challenge alone, because the API's URL carries nothing else -
    the same shape as `require_challenge_member`, and safe for the reason its
    docstring gives: with one id in the path there is no second key to disagree
    with, so the cross-check `require_challenge_editor` performs has nothing to
    check. It resolves through `effective_challenge_role`, so a site
    administrator is staff here exactly as they are everywhere.

    A plain `HTTPException` rather than `Forbidden`, which renders a page.
    """
    role = await effective_challenge_role(db, user, challenge_id)
    if role is None or role not in EDITOR_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only this challenge's course staff can do that.",
        )
    return role


ApiChallengeEditor = Annotated[UserRole, Depends(require_api_challenge_editor)]


async def authorize_submission(
    db: AsyncConnection, user: schema.User, submission
) -> bool:
    """Let the viewer see a submission, or raise 403. Returns True for staff.

    Not a dependency like `require_challenge_editor`, because it needs the
    submission row first and because a student *is* allowed here: the rule is
    that you may read your own work, and course staff may read anyone's.
    """
    role = await effective_challenge_role(db, user, submission["challenge_id"])
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
    await create_db_and_tables(dummy=CONFIG.dev.reset_db)


@app.get("/")
async def index(request: Request, db: DBDep, user: CurrentUser, q: str = ""):
    # Two affordances off one answer: "New course", and the Edit button on the
    # card of a course an administrator is not enrolled on. The routes behind
    # both check for themselves - see `require_admin` and
    # `effective_course_role`. It is also the third thing here: an archived
    # course is not listed to somebody who is not on it, and an administrator
    # is the only person who can unarchive one, so they have to be able to find
    # it.
    is_admin = await is_administrator(db, user.email)
    courses = await query_courses(db, user_id=user.id, q=q, include_archived=is_admin)
    return templates.TemplateResponse(
        request=request,
        name="index-logged-in.jinja",
        context={
            "courses": courses,
            "q": q,
            "title": "COCCO: Coding Challenges Companion",
            "user": user,
            "is_admin": is_admin,
        },
    )


async def _render_courses(
    request: Request, db: AsyncConnection, user: schema.User, q: str
):
    """Both halves of the courses list. Shared by the filter box's GET and by
    `post_course_enroll`, so the list a click leaves behind is the one the box
    would have produced."""
    # `_course-card.jinja` reads this: an administrator is staff on every
    # course, so offering them `Enroll!` on one would be odd. `query_courses`
    # still splits the two halves on enrollment alone - they really are not
    # enrolled, and the split should keep saying so. It also decides whether
    # the archived courses they are not on are listed at all; see `index`.
    is_admin = await is_administrator(db, user.email)
    courses = await query_courses(db, user_id=user.id, q=q, include_archived=is_admin)
    return templates.TemplateResponse(
        request=request,
        name="f-courses.jinja",
        context={
            "courses": courses,
            "q": q,
            "user": user,
            "is_admin": is_admin,
        },
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


########################################################################
# Administering the site. Administrators only, through `Admin` - the second
# thing that dependency guards, and the reason its sentence names the
# permission rather than creating a course.
#
# Unlike its neighbour above, the position of this block is not load-bearing:
# `/admin` is a literal segment that collides with no path parameter.
#
# Every mutation here answers with the user list, alert included, rather than
# with an alert alone: it is `post_course_enroll` answering with the courses
# list it changed, with `f-enroll.jinja`'s rule folded in - what you are
# looking at after a refusal is the thing you acted on. One swap target means
# the sentence and the new state arrive together instead of being kept in step.


async def _render_users(
    request: Request,
    db: AsyncConnection,
    user: schema.User,
    q: str,
    message: str | None = None,
    ok: bool = False,
):
    """The user list, as every route on the admin page answers with it."""
    users = await query_users(db, q=q)
    return templates.TemplateResponse(
        request=request,
        name="f-users.jinja",
        context={
            "users": users,
            "q": q,
            "user": user,
            "message": message,
            "ok": ok,
        },
    )


@app.get("/admin")
async def get_admin(request: Request, db: DBDep, user: Admin, q: str = ""):
    """The admin page: everyone the site knows about, and the way to appoint
    another administrator."""
    users = await query_users(db, q=q)
    return templates.TemplateResponse(
        request=request,
        name="admin.jinja",
        context={
            "users": users,
            "q": q,
            "title": "Administration",
            "user": user,
            "message": None,
            "ok": False,
        },
    )


@app.get("/f/users")
async def get_fragment_users(request: Request, db: DBDep, user: Admin, q: str = ""):
    """The user list, re-rendered as the filter box is typed in. The page
    inlines this same fragment on first paint."""
    return await _render_users(request, db, user, q)


@app.post("/admin/administrators")
async def post_administrator(
    request: Request,
    db: DBDep,
    user: Admin,
    # Defaulted rather than required, so an empty box becomes the alert below
    # instead of a 422 - which htmx would drop on the floor, leaving the person
    # who submitted it looking at nothing at all.
    email: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
):
    """Appoint a site administrator, by address.

    By address and not by user id, because that is what `administrator` holds:
    the point of the table is that it can name someone before they have ever
    signed in, and this form is the ordinary way that happens - a colleague is
    appointed, and their account appears on their first sign-in.

    This is the only place in the app where a human types an address, so it is
    the only place that has to normalize one. It also refuses an address outside
    `allowed_domains`, which `_load_auth_config` merely warns about: a bad line
    in `cocco.toml` must not stop the app from booting, but a form has somebody
    in front of it who can fix the typo.
    """
    try:
        email = normalize_email(email)
    except ValueError as err:
        return await _render_users(request, db, user, q, message=str(err))

    if not is_allowed_email(email, CONFIG.auth.allowed_domains):
        domains = ", ".join("@" + d for d in CONFIG.auth.allowed_domains)
        return await _render_users(
            request,
            db,
            user,
            q,
            message=(
                f"{email} could never sign in: administrators must have "
                f"an address at {domains}."
            ),
        )

    row = await query_user_by_email(db, email)
    if row is not None and row.blocked_at is not None:
        # The other half of "an administrator is never blocked". Appointing is
        # one-way through the app, so without this an administrator could be
        # made who cannot sign in and cannot be unmade.
        return await _render_users(
            request,
            db,
            user,
            q,
            message=f"{email} is blocked. Unblock them first.",
        )

    # ON CONFLICT DO NOTHING, so appointing someone twice is a no-op rather
    # than an error - and the message below is true either way.
    await insert_administrators(db, [email])
    await db.commit()
    return await _render_users(
        request, db, user, q, message=f"{email} is now an administrator.", ok=True
    )


@app.post("/admin/users/{user_id}/block")
async def post_user_block(
    request: Request,
    db: DBDep,
    user: Admin,
    user_id: int,
    reason: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
):
    """Shut an account out of the site.

    Refusing on an administrator is `block_user`'s job, not this route's, so
    that the rule and the write are one statement - see it for why. `False`
    comes back for an administrator and for an id that is not there, and both
    get the same sentence, because in both cases nothing happened.
    """
    reason = reason.strip()
    if not await block_user(db, user_id, reason or None):
        return await _render_users(
            request,
            db,
            user,
            q,
            message="Nobody was blocked: an administrator cannot be.",
        )
    await db.commit()
    return await _render_users(
        request, db, user, q, message="That account is now blocked.", ok=True
    )


@app.post("/admin/users/{user_id}/unblock")
async def post_user_unblock(
    request: Request,
    db: DBDep,
    user: Admin,
    user_id: int,
    q: Annotated[str, Form()] = "",
):
    """Let an account back in."""
    if not await unblock_user(db, user_id):
        return await _render_users(
            request, db, user, q, message="There is no such account."
        )
    await db.commit()
    return await _render_users(
        request, db, user, q, message="That account is welcome again.", ok=True
    )


async def _render_enroll(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    next: str = "",
    error: str | None = None,
):
    """The way into a course, as the preview shows it: the `Enroll!` button, and
    the key field when the course wants one.

    Shared by `get_course`, which inlines it on first paint, and by
    `post_course_enroll`, which swaps it back with a message when the key was
    wrong - so what a refused student is looking at is the same form they typed
    into. It is handed `requires_key` and never the key itself.
    """
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-enroll.jinja",
        context={"course": course, "next": next, "error": error},
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

    "Non-member" is `effective_course_role`'s answer, so a site administrator
    gets the course itself rather than an invitation to join it.
    """
    role = await effective_course_role(db, user, course_id)
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
    key: Annotated[str, Form()] = "",
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

    A course with an enrollment key set admits nobody who cannot quote it. The
    field is only on the preview - the home page's card links there instead of
    offering a button - so a wrong key is answered with that same fragment and
    a message, rather than with the courses list: 200 and not a 4xx, because
    htmx does not swap the body of an error response and the fragment is the
    entire point of the request.

    Otherwise two callers, so two answers. From the home page it renders the
    whole list, both halves, because the point of the click is that the course
    moves from "Other courses" up into "Your courses"; `q` comes back with it
    so enrolling under a typed filter does not silently reset the list. From a
    course preview there is no list to re-render - the page has to become the
    course page - so that form sends `next` and gets the `HX-Redirect` idiom
    `post_course_new` uses. `next` is filtered through `safe_next_path` like
    every other destination in the app: it arrives in a request, so it is not
    trusted to be a path on this site.
    """
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)

    # Before the key, because an archived course refuses whether or not the key
    # is right - it is not taking students at all. The same fragment the key
    # refusal answers with, for the same reason: what a refused student is
    # looking at is the panel they clicked in. In practice this is only ever
    # reached from a stale tab or a typed URL, since `query_courses` does not
    # offer an archived course to anybody who is not on it - which is exactly
    # why the route has to say it rather than the listing.
    if await query_course_archived_at(db, course_id) is not None:
        return await _render_enroll(
            request,
            db,
            course_id,
            next,
            error="This course is archived and is not taking new students.",
        )

    expected = await query_enrollment_key(db, course_id)
    if expected is not None and not hmac.compare_digest(key.strip(), expected):
        # Trimmed, because a key read off a slide is pasted with whitespace,
        # but otherwise exact - it is a string a teacher chose, and deciding
        # that case does not count is not ours to make. `compare_digest`
        # rather than `==` so the comparison does not time out early.
        return await _render_enroll(
            request, db, course_id, next, error="That key is not right."
        )

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
            # The danger zone at the foot of this page is administrators only,
            # and `require_course_editor` lets a plain teacher in here - so the
            # card has to ask its own question. The routes behind it are behind
            # `Admin`; this only decides whether it is drawn.
            "is_admin": await is_administrator(db, user.email),
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
    enrollment_key: Annotated[str, Form()] = "",
):
    """Save the course's title, description and enrollment key."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    title = title.strip()
    if not title:
        return alert("A course needs a title.")

    # An empty box is NULL, not the empty string: NULL is what "anyone may
    # join" is spelled as, and clearing the field has to reopen the course
    # rather than set a key nobody can type.
    key = enrollment_key.strip() or None
    await update_course(
        db,
        course_id,
        title=title,
        description=description,
        enrollment_key=key,
    )
    await db.commit()
    return alert(
        "Saved. Students need the enrollment key to join." if key else "Saved.",
        ok=True,
    )


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


########################################################################
# The course's danger zone: archiving it, and ending it. Site administrators
# only, through `Admin`, and deliberately *not* `CourseEditor` - which
# `get_course_edit` lets a plain teacher through. What the home page lists, and
# what exists at all, is the site's business rather than one course's; it is
# the same line `post_course_new` draws at the other end of a course's life,
# and the reason `require_admin`'s sentence names the permission rather than
# either of the places it is used - this is now the third.
#
# Declared here, above `get_challenge`, for the reason the editor above is:
# `/courses/{course_id}/{challenge_id}` matches any single segment. These are
# POSTs and would survive being declared below it today - a path match with the
# wrong method is only a partial match, and the router keeps looking - but that
# is an accident of the method rather than a rule, and the next GET added here
# would find out the hard way.
#
# Archive and unarchive are two routes rather than one with a flag, mirroring
# `/admin/users/{id}/block` and `/unblock`: the closest analogue in the app, a
# reversible state an administrator toggles, with each URL meaning one thing.
# Both answer with `HX-Redirect` back to the same editor rather than a fragment
# swap, because archiving changes the header badge, every challenge card's
# greying and the button's own label - the page *is* the one swap target.


def _danger_alert(request: Request, message: str, ok: bool = False):
    """The alert both danger zones swap in. The same fragment every other
    editor route answers with; it is the sentence that differs."""
    return templates.TemplateResponse(
        request=request,
        name="f-edit-result.jinja",
        context={"ok": ok, "message": message},
    )


def _judge_busy_message(running: int, what: str) -> str:
    """`post_regrade`'s refusal, worded for a delete. Spelled once because both
    danger zones refuse in exactly the same circumstances."""
    return (
        f"A judge is running {running} submission{'' if running == 1 else 's'} "
        f"on this {what} right now. Nothing was deleted — try again in a moment."
    )


def _confirmation_matches(typed: str, title: str) -> bool:
    """Whether the name typed into a delete dialog is the thing's own.

    Checked on the server and not only in the dialog: the script there merely
    *enables* the button, and a disabled control is presentation, never the
    check - the rule `require_challenge_editor` states for hidden buttons.

    A plain `==` and deliberately not `hmac.compare_digest`, unlike the
    enrollment key: the title is printed on the page the box sits on, so this
    confirms an intention rather than proving a secret.
    """
    return typed.strip() == title.strip()


@app.post("/courses/{course_id}/archive")
async def post_course_archive(course_id: int, request: Request, db: DBDep, user: Admin):
    """Archive a course: read-only for the people already on it, invisible and
    unjoinable to everybody else. Staff go on editing it - archiving is a
    visibility state, not a lock, which is what keeps the whole feature down to
    two enforcement points (`post_submission` and `post_course_enroll`)."""
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)
    await set_course_archived(db, course_id, True)
    await db.commit()
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(request.url_for("get_course_edit", course_id=course_id))
        },
    )


@app.post("/courses/{course_id}/unarchive")
async def post_course_unarchive(
    course_id: int, request: Request, db: DBDep, user: Admin
):
    """Undo the above. Clears the column rather than writing a second one - an
    archive that is over has no date."""
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)
    await set_course_archived(db, course_id, False)
    await db.commit()
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(request.url_for("get_course_edit", course_id=course_id))
        },
    )


@app.post("/courses/{course_id}/delete")
async def post_course_delete(
    course_id: int,
    request: Request,
    db: DBDep,
    user: Admin,
    confirm: Annotated[str, Form()] = "",
):
    """Delete a course, every challenge on it, every submission ever made to
    them and every file they own.

    `confirm` is the course's own title, typed into the dialog. It is a
    **defaulted** form field for `post_administrator`'s reason: a missing field
    is a 422, htmx drops the body of an error response, and the person who
    clicked would be left looking at nothing.

    Refusals are 200 with an alert, the success is a 204 carrying
    `HX-Redirect` - there is no page left to swap a fragment into.
    """
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)

    if not _confirmation_matches(confirm, course.title):
        return _danger_alert(
            request,
            "Type the course's title exactly as it is shown to delete it. "
            "Nothing was changed.",
        )

    # Refused while a judge is mid-run, as a regrade is - and for a sharper
    # reason. See `lock_challenge_submissions`: a submission deleted mid-run
    # takes the judge *process* down, where the same race against a regrade
    # only leaves one stale score. The lock is what makes this more than
    # usually true.
    running = await lock_course_submissions(db, course_id)
    if running:
        return _danger_alert(request, _judge_busy_message(running, "course"))

    # Read before the deletes, unlinked after the commit: the order
    # `post_testcase_delete` states. A failed commit can leave a file nothing
    # points at, but never a row pointing at a file that is gone.
    #
    # `_testcase_dir` and `_evaluator_dir` are defined further down this file,
    # with the challenge editor they belong to. That is fine - a name in a
    # function body is resolved when the body runs - and moving this route down
    # to be near them would put it below `get_challenge`, which is the one
    # thing it may not be.
    challenge_ids = await query_ordered_challenge_ids(db, course_id)
    paths: list[str] = []
    for challenge_id in challenge_ids:
        paths.extend(await query_challenge_file_paths(db, challenge_id))

    await delete_course(db, course_id)
    await db.commit()

    for path in paths:
        storage.delete(path)
    for challenge_id in challenge_ids:
        storage.delete_directory(_testcase_dir(challenge_id))
        storage.delete_directory(_evaluator_dir(challenge_id))

    return Response(
        status_code=204, headers={"HX-Redirect": str(request.url_for("index"))}
    )


########################################################################
# The course's people: who is enrolled, in what role, and the two ways that
# changes. Course staff only, through `CourseEditor` - and keyed on the course
# for the reason that dependency exists, so staff of one course are strangers
# on every other. A site administrator passes it without an enrollment, which
# is `effective_course_role`'s doing and not this block's.
#
# Declared here, above `get_challenge`, and that is load-bearing exactly as it
# is for the course editor: `/courses/{course_id}/{challenge_id}` matches any
# single segment, so a `/courses/1/people` declared after it would be routed
# there and rejected as an unparseable id before this ever ran.
#
# It is `/admin` reshaped for one course, and it keeps that page's rule: one
# swap target, and every route - the filter box's GET and all three mutations -
# answers with the whole list, alert folded in, so the sentence and the new
# state arrive together instead of being kept in step. Every mutation carries
# `q`, so acting under a filter leaves the filter in force.


async def _render_course_people(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    user: schema.User,
    q: str,
    message: str | None = None,
    ok: bool = False,
):
    """The roster, as every route on the people page answers with it."""
    people = await query_course_people(db, course_id, q=q)
    return templates.TemplateResponse(
        request=request,
        name="f-course-people.jinja",
        context={
            "people": people,
            "course_id": course_id,
            "q": q,
            "user": user,
            "message": message,
            "ok": ok,
        },
    )


@app.get("/courses/{course_id}/people")
async def get_course_people(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
):
    """The class list: everyone enrolled, in what role, and how much they have
    submitted here."""
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    people = await query_course_people(db, course_id)
    return templates.TemplateResponse(
        request=request,
        name="course-people.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "people": people,
            "q": "",
            "title": f"People on {course.title}",
            "user": user,
            "message": None,
            "ok": False,
        },
    )


@app.get("/f/courses/{course_id}/people")
async def get_fragment_course_people(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    q: str = "",
):
    """The roster, re-rendered as the filter box is typed in. The page inlines
    this same fragment on first paint."""
    return await _render_course_people(request, db, course_id, user, q)


@app.post("/courses/{course_id}/people")
async def post_course_person(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    email: Annotated[str, Form()] = "",
    new_role: Annotated[str, Form()] = UserRole.student.value,
    q: Annotated[str, Form()] = "",
):
    """Put somebody on the course by address, in whatever role staff chose.

    By address, like `post_administrator`, and for half of its reason: this is
    where a teacher types out a roster, and most of those people have not signed
    in yet. Unlike that table, `enrollment` is keyed on `user.id` and so cannot
    name an address with no account behind it - which is why the account is
    *created* here, through the same `get_or_create_user` the Google callback
    uses. The row then sits waiting, and their first sign-in finds it.

    That makes this the second place in the app where a human types an address,
    so it is the second that has to normalize one and refuse an address outside
    `allowed_domains` - a pre-enrolled student who could never sign in is a typo
    worth catching at the form, where somebody is standing who can fix it.

    A blocked account is deliberately *not* refused, unlike an appointment: that
    one is one-way through the app, while this is undone by the button in the
    row it creates - and a block stops a person without retracting the fact that
    they are on the course. The row says `blocked` and that is the honest answer.
    """
    try:
        email = normalize_email(email)
    except ValueError as err:
        return await _render_course_people(
            request, db, course_id, user, q, message=str(err)
        )

    if not is_allowed_email(email, CONFIG.auth.allowed_domains):
        domains = ", ".join("@" + d for d in CONFIG.auth.allowed_domains)
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                f"{email} could never sign in: this server only admits "
                f"an address at {domains}."
            ),
        )

    # Through the enum, never as text - the same rule the leaderboard criterion
    # follows. A role is not something a form field gets to spell.
    try:
        wanted = UserRole(new_role)
    except ValueError:
        return await _render_course_people(
            request, db, course_id, user, q, message="That is not a role."
        )

    # Before the account is created, so a POST naming a course that is not there
    # does not leave a stray user behind. `lock_course` is the existence check
    # and the serialization in one - see it for why every mutation takes it.
    if not await lock_course(db, course_id):
        raise HTTPException(status_code=404)

    person = await get_or_create_user(db, email)
    # Only to word the sentence below; the write is the same either way, and a
    # race here changes an adjective and nothing else.
    before = await query_course_role(db, user_id=person.id, course_id=course_id)

    if not await set_enrollment_role(db, course_id, person.id, wanted):
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                f"{email} is this course's last teacher, so they cannot be "
                "made anything else. Make somebody else a teacher first."
            ),
        )
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    if before is None:
        message = f"{email} is now a {wanted.value} on this course."
    elif before is wanted:
        message = f"{email} was already a {wanted.value} on this course."
    else:
        message = f"{email} is a {wanted.value} now, not a {before.value}."
    return await _render_course_people(
        request, db, course_id, user, q, message=message, ok=True
    )


@app.post("/courses/{course_id}/people/{user_id}/role")
async def post_course_person_role(
    course_id: int,
    user_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    new_role: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
):
    """Change one person's role on the course.

    The same write as the form above - `set_enrollment_role` is one upsert
    behind both, because `enrollment` is keyed on (user_id, course_id) and so
    "enroll them as a teacher" and "make them a teacher" name one row. What is
    refused is the same too: a course must not lose its last teacher, and a
    demotion is one of the two ways that could happen.
    """
    try:
        wanted = UserRole(new_role)
    except ValueError:
        return await _render_course_people(
            request, db, course_id, user, q, message="That is not a role."
        )

    if not await lock_course(db, course_id):
        raise HTTPException(status_code=404)

    if not await set_enrollment_role(db, course_id, user_id, wanted):
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                "Nobody was changed: a course cannot lose its last teacher. "
                "Make somebody else a teacher first."
            ),
        )
    await db.commit()
    return await _render_course_people(
        request,
        db,
        course_id,
        user,
        q,
        message=f"That role is now {wanted.value}.",
        ok=True,
    )


@app.post("/courses/{course_id}/people/{user_id}/remove")
async def post_course_person_remove(
    course_id: int,
    user_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    q: Annotated[str, Form()] = "",
):
    """Take somebody off the course.

    Two refusals, and they sit in different places on purpose. Removing
    *yourself* is checked here, because it is not a race - nobody becomes
    somebody else between two statements - and because it deserves its own
    sentence: it would shut the door on the very page you are standing on.
    Removing the last teacher is checked inside the `DELETE`'s WHERE clause, the
    way `block_user` refuses an administrator, so the rule and the write cannot
    be told apart by a demotion landing between them.

    Nothing of theirs goes with the row: their submissions, outcomes and
    leaderboard entries stay exactly where they are. What they lose is access
    and their place in the standings - see `delete_enrollment`.
    """
    if user_id == user.id:
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                "You cannot remove yourself from a course you are staff on. "
                "Ask another teacher, or a site administrator."
            ),
        )

    if not await lock_course(db, course_id):
        raise HTTPException(status_code=404)

    if not await delete_enrollment(db, course_id, user_id):
        # False for the last teacher and for a row that is not there, and both
        # get this sentence, because in both cases nothing happened.
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                "Nobody was removed: a course cannot lose its last teacher."
            ),
        )
    await db.commit()
    return await _render_course_people(
        request,
        db,
        course_id,
        user,
        q,
        message="They are off the course. Their submissions are untouched.",
        ok=True,
    )


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
    # The dependency's role rather than the query's `viewer_role`: that one is a
    # LEFT JOIN on `enrollment` and is NULL for a site administrator who is not
    # enrolled, which would hand them the student page and a submit form.
    can_edit = role in EDITOR_ROLES
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

    # One of the two places archiving is enforced, the other being
    # `post_course_enroll`. Before the upload is read, so an archived challenge
    # does not buffer a megabyte only to refuse it, and read through
    # `query_effective_archived_at` so that archiving the *course* closes this
    # too - the submit form is already gone from the page, and this is the
    # check behind it.
    if await query_effective_archived_at(db, challenge_id) is not None:
        return alert(
            "This challenge is archived. You can still read it and look back "
            "over your attempts, but it is not taking new submissions."
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
# The challenge's danger zone. `ChallengeEditor` is exactly "administrators and
# course staff" and needs nothing added: it depends on `CourseEditor`, which
# resolves through `effective_course_role`, so an administrator is staff here
# without an enrollment - and it 404s a challenge that is not under the course
# in the path, the cross-check that stops staff of one course naming their own
# in front of somebody else's challenge.
#
# No route-ordering question here, unlike the course's: these paths have a
# third segment, so `get_challenge` cannot swallow them.


@app.post("/courses/{course_id}/{challenge_id}/archive")
async def post_challenge_archive(
    course_id: int, challenge_id: int, request: Request, db: DBDep, role: ChallengeEditor
):
    """Archive one challenge: still readable by the class, no longer accepting
    submissions. Archiving its *course* does the same to it without touching
    this column - see `_EFFECTIVE_ARCHIVED_SQL`."""
    await set_challenge_archived(db, challenge_id, True)
    await db.commit()
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


@app.post("/courses/{course_id}/{challenge_id}/unarchive")
async def post_challenge_unarchive(
    course_id: int, challenge_id: int, request: Request, db: DBDep, role: ChallengeEditor
):
    """Undo the above. If the course itself is archived the challenge stays
    read-only, which is what the editor's note beside this button says."""
    await set_challenge_archived(db, challenge_id, False)
    await db.commit()
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


@app.post("/courses/{course_id}/{challenge_id}/delete")
async def post_challenge_delete(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    confirm: Annotated[str, Form()] = "",
):
    """Delete a challenge, its test cases, every submission to it and its
    files. `post_course_delete`'s body for one challenge; the reasoning behind
    each step is written out there.

    It lands on the course editor, which is the list the challenge has just
    left.
    """
    challenge = await query_challenge_config(db, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404)

    if not _confirmation_matches(confirm, challenge.title):
        return _danger_alert(
            request,
            "Type the challenge's title exactly as it is shown to delete it. "
            "Nothing was changed.",
        )

    running = await lock_challenge_submissions(db, challenge_id)
    if running:
        return _danger_alert(request, _judge_busy_message(running, "challenge"))

    paths = await query_challenge_file_paths(db, challenge_id)
    await delete_challenge(db, challenge_id)
    await db.commit()

    for path in paths:
        storage.delete(path)
    storage.delete_directory(_testcase_dir(challenge_id))
    storage.delete_directory(_evaluator_dir(challenge_id))

    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(request.url_for("get_course_edit", course_id=course_id))
        },
    )


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


def _check_evaluator_source(name: str, contents: bytes) -> str | None:
    """None if these bytes may become a challenge's evaluator, else the reason.

    One rule with two callers - the editor's upload form and the API's push -
    because a script the browser accepted and the CLI refused (or worse, the
    other way round) would be a difference nobody could explain. Every check
    here is about the file itself, so neither caller needs to know the other.
    """
    if len(contents) > MAX_EVALUATOR_BYTES:
        return (
            f"That file is {len(contents) / 1024:.0f} KB - the limit is "
            f"{MAX_EVALUATOR_BYTES // 1024} KB. An evaluator is a script, not "
            "test data."
        )

    # Load-bearing, not cosmetic: the extension is the only thing that tells
    # the judge how to compile and run the script, so a name it cannot read is
    # refused here rather than becoming an evaluator_error on every submission.
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if EXTENSION_TO_LANGUAGE.get(extension) is None:
        known = ", ".join(sorted("." + e for e in EXTENSION_TO_LANGUAGE))
        return f"Cannot tell the language of {name!r}. Accepted: {known}."

    try:
        contents.decode("utf-8")
    except UnicodeDecodeError:
        return f"{name} is not valid UTF-8 text."

    return None


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
    # Size, extension and encoding, shared with the API's push so the two
    # cannot disagree about what a usable evaluator is.
    problem = _check_evaluator_source(name, contents)
    if problem is not None:
        return await refuse(problem)
    language = EXTENSION_TO_LANGUAGE[name.rsplit(".", 1)[-1].lower()]

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


# ═══════════════════════════════════════════════════════════════════════════
# The profile
# ═══════════════════════════════════════════════════════════════════════════
#
# One URL and two pages, the way `get_challenge` is: `/users/{id}` is the
# profile, and who is asking decides whether the fields are text or a form.
# The owner may change everything but the address; a site administrator reads
# the same page and cannot touch it.
#
# That asymmetry is the point, and it is why there are two dependencies rather
# than one. `/admin` is about what an account may *do* - appoint it, block it -
# and those are powers over somebody. What they are called and what their face
# looks like is not a power, it is theirs; an administrator who needs to change
# it can look at the page and ask. The address is nobody's to edit: it is the
# login identifier Google proves, and `user.email` is unique on it.
#
# None of these routes collide with a path parameter elsewhere, so their
# position in this file is not load-bearing the way `/courses/new`'s is.


async def require_profile_reader(user_id: int, db: DBDep, user: CurrentUser) -> bool:
    """Authorize a look at a profile, and answer whose it is.

    Returns `can_edit`, so the route asks once and the template does not have
    to work it out again - the same trick `authorize_submission` plays with
    `is_staff`. A profile is one real person's name, face and class list, so
    the two who may see it are that person and a site administrator; being
    course staff somewhere they happen to be enrolled is not enough, because
    the page spans every course rather than the one in a URL.
    """
    if user.id == user_id:
        return True
    if await is_administrator(db, user.email):
        return False
    raise Forbidden("You can only look at your own profile.")


ProfileReader = Annotated[bool, Depends(require_profile_reader)]


async def require_profile_owner(user_id: int, user: CurrentUser) -> schema.User:
    """Authorize a change to a profile: only ever your own.

    Deliberately without the administrator branch `require_profile_reader` has.
    Reading somebody's profile is oversight; renaming them is impersonation,
    and there is no button on any page that asks for it.
    """
    if user.id != user_id:
        raise Forbidden("You can only edit your own profile.")
    return user


ProfileOwner = Annotated[schema.User, Depends(require_profile_owner)]

# The longest nickname the navbar can show without wrapping, and the same
# ceiling `nickname_from_email` truncates to when it derives one.
MAX_NICKNAME_CHARS = 64


async def _render_profile_identity(
    request: Request,
    db: AsyncConnection,
    user_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The name and the face, as every mutation on the profile answers with it.

    One swap target, alert included, for the reason `/admin` has one: the
    sentence and the new state arrive together instead of being kept in step.
    It covers exactly what a mutation here can change - deliberately not the
    address, the badges or the courses, none of which any button on this page
    touches, and all of which would drag `query_user_profile` in behind it.
    """
    row = await query_user(db, user_id)
    if row is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-profile-identity.jinja",
        context={
            "profile": row,
            # Every route that renders this fragment is behind `ProfileOwner`.
            "can_edit": True,
            "message": message,
            "ok": ok,
        },
    )


@app.get("/users/{user_id}")
async def get_profile(
    user_id: int, request: Request, db: DBDep, user: CurrentUser, can_edit: ProfileReader
):
    """What the site knows about one person: their address, their name, their
    face and the courses they are on."""
    profile = await query_user_profile(db, user_id)
    if profile is None:
        raise HTTPException(status_code=404)
    # `can_edit` is the owner *or* an administrator; the token section is the
    # owner alone, which is a narrower question than the page has asked so far
    # and so needs its own answer. Both are presentation - the routes behind
    # every form here are guarded by `ProfileOwner` regardless.
    is_owner = user.id == user_id
    return templates.TemplateResponse(
        request=request,
        name="profile.jinja",
        context={
            "profile": profile,
            "can_edit": can_edit,
            "is_owner": is_owner,
            "profile_id": user_id,
            "tokens": await query_api_tokens(db, user_id) if is_owner else [],
            "minted": None,
            "title": profile["nickname"],
            "user": user,
            "message": None,
            "ok": False,
        },
    )


@app.post("/users/{user_id}/profile")
async def post_profile(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    # Defaulted rather than required, as on `/admin`: a missing field is a 422,
    # and htmx drops the body of an error response, so the person who cleared
    # the box would see nothing at all instead of the sentence below.
    nickname: Annotated[str, Form()] = "",
):
    """Save the display name.

    The avatar is not regenerated to match. It is seeded on the nickname when
    the account is created, but once somebody has a face it is theirs and a
    rename must not silently redraw it - the button below is how it changes.
    """
    nickname = nickname.strip()
    if not nickname:
        return await _render_profile_identity(
            request, db, user_id, message="A profile needs a name."
        )
    if len(nickname) > MAX_NICKNAME_CHARS:
        return await _render_profile_identity(
            request,
            db,
            user_id,
            message=f"That name is longer than {MAX_NICKNAME_CHARS} characters.",
        )

    await update_user(db, user_id, nickname=nickname)
    await db.commit()
    return await _render_profile_identity(request, db, user_id, "Saved.", ok=True)


@app.post("/users/{user_id}/avatar")
async def post_avatar(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    avatar_file: Annotated[UploadFile, File()],
):
    """Replace the avatar with an uploaded picture.

    The mime stored beside the bytes is `sniff_image_mime`'s answer and never
    the upload's own `content_type` header, which is written by whoever is
    uploading: `user_avatar` below serves those bytes back under that mime from
    this origin, so believing the header would be believing a stranger about
    what the browser should execute.
    """

    async def refuse(message: str):
        return await _render_profile_identity(request, db, user_id, message=message)

    def too_big(size: int) -> str:
        return (
            f"That picture is {size / 1024:.0f} KB — the limit is "
            f"{MAX_AVATAR_BYTES // 1024} KB."
        )

    # Checked before the read, so a huge upload is refused rather than
    # buffered. `size` may be absent, hence the second check below.
    if avatar_file.size is not None and avatar_file.size > MAX_AVATAR_BYTES:
        return await refuse(too_big(avatar_file.size))

    contents = await avatar_file.read()
    if len(contents) > MAX_AVATAR_BYTES:
        return await refuse(too_big(len(contents)))
    if not contents:
        return await refuse("That file is empty.")

    mime = sniff_image_mime(contents)
    if mime is None:
        return await refuse("That is not a PNG, JPEG, GIF or WebP picture.")

    await update_user(db, user_id, image=contents, image_mime=mime)
    await db.commit()
    return await _render_profile_identity(
        request, db, user_id, "That is your avatar now.", ok=True
    )


@app.post("/users/{user_id}/avatar/reset")
async def post_avatar_reset(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    """Go back to the generated identicon.

    Seeded on the nickname as it is *now*, not as it was when the account was
    created, so this doubles as the way to make the face follow a rename.
    """
    image, image_mime = generate_identicon(owner.nickname)
    await update_user(db, user_id, image=image, image_mime=image_mime)
    await db.commit()
    return await _render_profile_identity(
        request, db, user_id, "Your avatar is generated from your name again.", ok=True
    )


########################################################################
# API tokens, which live on the profile because they are the account's, not the
# site's. Every route here is behind `ProfileOwner` and deliberately *not*
# `ProfileReader`, the read half - which is the same line that page already
# draws, one step further along. Reading a profile is oversight; minting a
# credential in somebody else's name is impersonation, and even *listing*
# somebody's tokens is reading the shape of their credentials rather than
# anything about them. An administrator who needs one of these gone has the
# same escape hatch the other two have: `DELETE FROM api_token WHERE ...`.

# Long enough to say what a token is for, short enough for the table.
MAX_TOKEN_LABEL_CHARS = 60


async def _render_tokens(
    request: Request,
    db: AsyncConnection,
    user_id: int,
    message: str | None = None,
    ok: bool = False,
    minted: str | None = None,
):
    """The token list, as every mutation answers with it.

    One swap target with the alert folded in, which is `/admin`'s rule. `minted`
    is the one thing this fragment shows that no query can produce: the token
    itself, on the single response that created it. Only its hash is stored, so
    there is nowhere it could be read back from - which is the point, and which
    is why the fragment says so where it prints it.
    """
    return templates.TemplateResponse(
        request=request,
        name="f-tokens.jinja",
        context={
            "profile_id": user_id,
            "tokens": await query_api_tokens(db, user_id),
            "message": message,
            "ok": ok,
            "minted": minted,
        },
    )


@app.get("/f/users/{user_id}/tokens")
async def get_fragment_tokens(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    return await _render_tokens(request, db, user_id)


@app.post("/users/{user_id}/tokens")
async def post_token(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    # Defaulted rather than required, as on `/admin` and on the profile form
    # above: a missing field is a 422, and htmx drops the body of an error
    # response, so the person who left the box empty would see nothing at all.
    label: Annotated[str, Form()] = "",
):
    """Mint a token, and show it once."""
    label = label.strip()
    if not label:
        return await _render_tokens(
            request, db, user_id, "Give the token a label, so you know what to "
            "revoke later.", ok=False,
        )
    if len(label) > MAX_TOKEN_LABEL_CHARS:
        return await _render_tokens(
            request, db, user_id,
            f"Labels are at most {MAX_TOKEN_LABEL_CHARS} characters.", ok=False,
        )

    token = mint_token()
    await insert_api_token(db, user_id, label, hash_token(token))
    await db.commit()
    return await _render_tokens(
        request, db, user_id,
        "Token created. Copy it now - it is not shown again.", ok=True,
        minted=token,
    )


@app.post("/users/{user_id}/tokens/{token_id}/delete")
async def post_token_delete(
    user_id: int,
    token_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
):
    """Revoke a token. It stops working on the very next call, because the
    lookup in `get_api_user` is a read of the row this deletes."""
    removed = await delete_api_token(db, user_id, token_id)
    await db.commit()
    return await _render_tokens(
        request, db, user_id,
        "Token revoked." if removed else "That token is already gone.",
        ok=removed,
    )


@app.get("/users/{user_id}/avatar")
async def user_avatar(user_id: int, db: DBDep, user: CurrentUser):
    row = (
        await db.execute(
            select(User.c.image, User.c.image_mime).where(User.c.id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404)
    return Response(
        content=row.image,
        media_type=row.image_mime,
        headers={
            # These bytes were uploaded by a user and are served from the same
            # origin as the app, so the browser is told not to guess at their
            # type and not to let them reach anything. `sniff_image_mime`
            # already refuses everything but the four raster formats; this is
            # the second lock on the same door.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # The URL does not change when the picture does, so without this a
            # new avatar would keep showing as the old one until a hard reload.
            "Cache-Control": "no-cache",
        },
    )



# ═══════════════════════════════════════════════════════════════════════════
# The transfer API
# ═══════════════════════════════════════════════════════════════════════════
#
# A challenge is otherwise only reachable one HTTP form at a time: one upload
# per test case, one arrow-press per reorder, a `<textarea>` for the statement.
# These four routes are the other way in - pull a challenge as a zip, edit it
# with an editor, push it back - and `cocco/cli.py` is what drives them.
#
# Everything here answers JSON or a zip, and refuses with a real status code
# rather than the app's "200 with an alert in the body". That convention exists
# because htmx will not swap the body of an error response; there is no htmx
# here, and a program that cannot tell a refusal from a success by its status
# is a program that will overwrite a challenge and report that it worked.
#
# The format itself is `cocco/transfer.py`, which touches nothing - so what a
# challenge looks like as files is testable without a database or a request.


def _slug(text: str) -> str:
    """A file-name-safe stub of a title, for the download's name only.

    Nothing is ever looked up by it: the challenge a push lands on is the one
    in the URL. It exists so a directory full of pulled challenges can be read.
    """
    stub = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return stub[:60] or "challenge"


def _existing_testcases(rows) -> list[ExistingTestCase]:
    """`query_challenge_testcases`' rows, reduced to what a plan needs.

    The narrowing is the point: `plan_testcases` decides the one rule that must
    not be got wrong - retire what has been graded, delete only what has not -
    and it does so over plain dataclasses, so it can be tested without either a
    database or a filesystem.
    """
    return [
        ExistingTestCase(
            id=row.id,
            input_name=row.input_path.rsplit("/", 1)[-1],
            expected_output_name=row.expected_output_path.rsplit("/", 1)[-1],
            is_sample=row.is_sample,
            is_active=row.is_active,
            n_outcomes=row.n_outcomes,
        )
        for row in rows
    ]


@app.get("/api/challenges")
async def api_list_challenges(db: DBDep, user: ApiUser):
    """Every challenge the caller may edit, so an id can be found from a shell.

    Not behind a course in the path, because the question a CLI asks first is
    "what can I edit?", which names no course. A site administrator is staff on
    every course, so they get all of them - `every_course` says that in the
    query rather than making the route filter afterwards.
    """
    rows = await query_editable_challenges(
        db,
        user_id=user.id,
        editor_roles=EDITOR_ROLES,
        every_course=await is_administrator(db, user.email),
    )
    return JSONResponse(
        [
            {
                "course_id": row.course_id,
                "course_title": row.course_title,
                "challenge_id": row.challenge_id,
                "title": row.title,
                # The *effective* state, so a challenge under an archived
                # course is marked too. A push to either is still accepted -
                # the API is the editor by another route, and staff go on
                # editing an archived challenge - so this is a warning to
                # somebody about to push to last year's course, not a gate.
                "archived": row.archived_at is not None,
            }
            for row in rows
        ]
    )


@app.get("/api/challenges/{challenge_id}")
async def api_export_challenge(
    challenge_id: int, db: DBDep, role: ApiChallengeEditor
):
    """The whole challenge as a zip: manifest, statement, test data, evaluator.

    Retired and hidden test cases are **included**, carrying their flags. This
    is the staff view - the same reason `query_challenge_testcases` does not
    filter `is_active` while the challenge page's `samples` CTE does - and a
    round trip that silently dropped every retired case would lose data on the
    way back in.

    The zip is built into a temporary file rather than into memory: one test
    case may be 32 MB and there is no limit on how many there are. The file is
    unlinked by a background task once the response has been sent.
    """
    challenge = await query_challenge_config(db, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404)

    rows = await query_challenge_testcases(db, challenge_id)
    evaluator_path = await query_evaluator_path(db, challenge_id)
    evaluator_name = (
        None if evaluator_path is None else evaluator_path.rsplit("/", 1)[-1]
    )

    manifest = Manifest(
        title=challenge.title,
        ranking=LeaderboardRanking(challenge.ranking),
        max_time_s=challenge.max_time_s,
        max_memory_mb=challenge.max_memory_bytes / 1024 / 1024,
        max_threads=challenge.max_threads,
        evaluator=(
            None if evaluator_name is None else f"{EVALUATOR_DIR}/{evaluator_name}"
        ),
        testcases=tuple(
            ManifestTestCase(
                input=f"{TESTS_DIR}/{row.input_path.rsplit('/', 1)[-1]}",
                expected_output=(
                    f"{TESTS_DIR}/{row.expected_output_path.rsplit('/', 1)[-1]}"
                ),
                is_sample=row.is_sample,
                is_active=row.is_active,
            )
            for row in rows
        ),
    )

    # A row may name a file that is not there - a failed commit after a write,
    # a storage root that moved, a database restored without its files. Asked
    # before the zip is opened, so the answer is a sentence naming the file
    # rather than a traceback halfway through a half-written download.
    for row in rows:
        for path in (row.input_path, row.expected_output_path):
            if not storage.exists(path):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This challenge cannot be exported: {path} is missing "
                        "from storage. The test case's row survived its file."
                    ),
                )
    if evaluator_path is not None and not storage.exists(evaluator_path):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This challenge cannot be exported: its evaluator "
                f"{evaluator_path} is missing from storage."
            ),
        )

    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                MANIFEST_NAME,
                dump_manifest(
                    manifest,
                    transfer.HEADER.format(
                        challenge_id=challenge_id,
                        statement=STATEMENT_NAME,
                        tests=TESTS_DIR,
                    ),
                ),
            )
            zf.writestr(STATEMENT_NAME, challenge.description or "")
            for row, entry in zip(rows, manifest.testcases):
                zf.write(storage.resolve(row.input_path), entry.input)
                zf.write(
                    storage.resolve(row.expected_output_path),
                    entry.expected_output,
                )
            if evaluator_path is not None and manifest.evaluator is not None:
                zf.write(storage.resolve(evaluator_path), manifest.evaluator)
        handle.close()
    except (OSError, ValueError):
        handle.close()
        os.unlink(handle.name)
        raise

    return FileResponse(
        handle.name,
        media_type="application/zip",
        filename=f"challenge-{challenge_id}-{_slug(challenge.title)}.zip",
        background=BackgroundTask(os.unlink, handle.name),
    )


def _read_archive(handle) -> tuple[zipfile.ZipFile, Manifest, str]:
    """Open a pushed zip and take the manifest and the statement out of it.

    Two caps, and they are not the same cap. The upload's own bytes are counted
    as they stream in, before this; here the *uncompressed* sizes declared in
    the zip's directory are summed before a single member is read, because a
    limit on compressed size alone is not a limit - that is what a zip bomb is.
    """
    try:
        archive = zipfile.ZipFile(handle)
    except zipfile.BadZipFile as e:
        raise ManifestError(f"That is not a zip file: {e}") from e

    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_TRANSFER_BYTES:
        raise ManifestError(
            f"The archive unpacks to {total / (1 << 20):.0f} MB - the limit is "
            f"{MAX_TRANSFER_BYTES >> 20} MB."
        )

    names = set(archive.namelist())
    if MANIFEST_NAME not in names:
        raise ManifestError(f"The archive has no {MANIFEST_NAME}.")

    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode("utf-8"))

    # An absent README is an empty statement rather than a refusal: a challenge
    # with no description is a challenge the editor lets you save today.
    statement = (
        archive.read(STATEMENT_NAME).decode("utf-8")
        if STATEMENT_NAME in names
        else ""
    )

    # Every file the manifest names has to be here...
    wanted = {tc.input for tc in manifest.testcases}
    wanted |= {tc.expected_output for tc in manifest.testcases}
    if manifest.evaluator is not None:
        wanted.add(manifest.evaluator)
    missing = sorted(wanted - names)
    if missing:
        raise ManifestError(
            "The archive is missing " + ", ".join(missing) + "."
        )

    # ...and every file under tests/ has to be named by it. Ignoring a stray
    # one silently is how somebody loses a test case they thought they added.
    stray = sorted(
        name
        for name in names
        if name.startswith(f"{TESTS_DIR}/")
        and not name.endswith("/")
        and name not in wanted
    )
    if stray:
        raise ManifestError(
            ", ".join(stray)
            + f" {'is' if len(stray) == 1 else 'are'} in the archive but not in "
            f"{MANIFEST_NAME}. Add a [[testcase]] block, or remove the file."
        )

    return archive, manifest, statement


@app.post("/api/challenges/{challenge_id}")
async def api_import_challenge(
    challenge_id: int,
    db: DBDep,
    role: ApiChallengeEditor,
    file: Annotated[UploadFile, File()],
):
    """Apply a pushed zip to the challenge named in the URL.

    Deliberately an update and never a create: the challenge a push lands on is
    the one in the path, so a manifest carries no id and a directory copied
    between checkouts cannot overwrite something it was not pointed at. A new
    challenge is still made in the web editor, and pushed to once it exists.

    The order of operations is the one the rest of the app uses - files written,
    the database updated, one commit, and only then the superseded files
    removed. A failed commit can therefore leave a file nothing points at, but
    never a row pointing at a file that is gone.
    """

    def refuse(message: str):
        return JSONResponse({"detail": message}, status_code=400)

    # Streamed to disk, not read: a push carries every test case at once.
    upload = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        received = 0
        while chunk := await file.read(1 << 20):
            received += len(chunk)
            if received > MAX_TRANSFER_BYTES:
                return refuse(
                    f"The upload is larger than {MAX_TRANSFER_BYTES >> 20} MB."
                )
            upload.write(chunk)
        upload.flush()
        upload.seek(0)

        try:
            archive, manifest, statement = _read_archive(upload)
        except ManifestError as e:
            return refuse(str(e))

        # The limits and the ranking are validated by the same function the
        # create form and the edit form go through, rather than a second time
        # here: this is `_challenge_fields`' third caller, not its rival.
        fields, error = _challenge_fields(
            statement,
            manifest.max_time_s,
            manifest.max_memory_mb,
            manifest.max_threads,
            manifest.ranking.value,
        )
        if fields is None:
            return refuse(error or "Invalid challenge.")

        # Read and checked here rather than where it is written, so that every
        # refusal this push can make happens before a single byte is stored.
        # An evaluator is capped at 1 MB, so reading it is not the streaming
        # question a test case is.
        evaluator_name = manifest.evaluator_name
        evaluator_source: bytes | None = None
        if manifest.evaluator is not None and evaluator_name is not None:
            evaluator_source = archive.read(manifest.evaluator)
            problem = _check_evaluator_source(evaluator_name, evaluator_source)
            if problem is not None:
                return refuse(problem)

        existing = _existing_testcases(
            await query_challenge_testcases(db, challenge_id)
        )
        plan = plan_testcases(existing, manifest)

        # A retired test case keeps its two files, so a name the manifest reuses
        # would be written on top of them. Checked before anything is written,
        # because afterwards it is somebody else's data that is gone.
        kept: dict[str, str] = {}
        for tc in plan.retired:
            for name in (tc.input_name, tc.expected_output_name):
                kept[name] = tc.input_name
        clash = sorted(
            {
                name
                for entry in manifest.testcases
                for name in (entry.input_name, entry.expected_output_name)
                if name in kept
            }
        )
        if clash:
            return refuse(
                ", ".join(clash)
                + " already belongs to a retired test case that this push does "
                "not name. Retired test cases keep their files - rename yours, "
                "or keep the [[testcase]] block."
            )

        with archive:
            directory = _testcase_dir(challenge_id)
            written: set[str] = set()

            def store(member: str, name: str) -> str:
                path = f"{directory}/{name}"
                with archive.open(member) as source:
                    storage.save_stream(path, source)
                written.add(path)
                return path

            superseded: list[str] = []

            for update in plan.updated:
                store(update.entry.input, update.entry.input_name)
                output_path = store(
                    update.entry.expected_output, update.entry.expected_output_name
                )
                if update.existing.expected_output_name != update.entry.expected_output_name:
                    superseded.append(
                        f"{directory}/{update.existing.expected_output_name}"
                    )
                await update_testcase(
                    db,
                    update.existing.id,
                    expected_output_path=output_path,
                    is_sample=update.entry.is_sample,
                    is_active=update.entry.is_active,
                )

            added_ids: list[int] = []
            for addition in plan.added:
                input_path = store(addition.entry.input, addition.entry.input_name)
                output_path = store(
                    addition.entry.expected_output,
                    addition.entry.expected_output_name,
                )
                new_id = await insert_testcase(
                    db,
                    challenge_id,
                    input_path,
                    output_path,
                    is_sample=addition.entry.is_sample,
                )
                # `insert_testcase` can express neither `is_active` nor an
                # explicit order - it always appends, which is the contract the
                # editor's Add button depends on - so the rest is a follow-up
                # rather than a widening of the helper.
                await update_testcase(db, new_id, is_active=addition.entry.is_active)
                added_ids.append(new_id)

            for tc in plan.retired:
                await update_testcase(db, tc.id, is_active=False)

            for tc in plan.deleted:
                await delete_testcase(db, tc.id)

            # `execution_order` is rewritten densely from the block order, which
            # is what makes reordering a matter of moving lines in a text file.
            # Through negative sentinels for `reorder_challenges`' reason: the
            # column may yet gain a unique constraint per challenge, and then
            # assigning the final numbers one at a time would collide with the
            # rows not rewritten yet. Retired cases go after the live ones.
            ordered: list[int | None] = [None] * len(manifest.testcases)
            for update in plan.updated:
                ordered[update.execution_order] = update.existing.id
            for addition, new_id in zip(plan.added, added_ids):
                ordered[addition.execution_order] = new_id
            live = [tid for tid in ordered if tid is not None]
            live += [tc.id for tc in plan.retired]
            for index, testcase_id in enumerate(live):
                await update_testcase(db, testcase_id, execution_order=-(index + 1))
            for index, testcase_id in enumerate(live):
                await update_testcase(db, testcase_id, execution_order=index)

            previous_evaluator = await query_evaluator_path(db, challenge_id)
            if evaluator_source is not None and evaluator_name is not None:
                new_evaluator = f"{_evaluator_dir(challenge_id)}/{evaluator_name}"
                storage.save_stream(new_evaluator, io.BytesIO(evaluator_source))
                await update_challenge(db, challenge_id, evaluator_path=new_evaluator)
                if previous_evaluator not in (None, new_evaluator):
                    superseded.append(str(previous_evaluator))
            else:
                # No `evaluator` key means the bundled default, exactly as the
                # editor's "Reset to default" does.
                await update_challenge(db, challenge_id, evaluator_path=None)
                if previous_evaluator is not None:
                    superseded.append(previous_evaluator)

            await update_challenge(
                db, challenge_id, title=manifest.title, **fields
            )
            await db.commit()

        # Only now, and never a path this push just wrote: a new test case is
        # free to take the name of one being deleted, and deleting afterwards
        # would then remove the file that replaced it.
        for tc in plan.deleted:
            for name in (tc.input_name, tc.expected_output_name):
                superseded.append(f"{directory}/{name}")
        for path in superseded:
            if path not in written:
                storage.delete(path)
    finally:
        upload.close()
        os.unlink(upload.name)

    return JSONResponse(
        {
            "challenge_id": challenge_id,
            "title": manifest.title,
            "testcases": {
                "updated": len(plan.updated),
                "added": len(plan.added),
                "retired": [tc.input_name for tc in plan.retired],
                "deleted": [tc.input_name for tc in plan.deleted],
            },
            "evaluator": evaluator_name,
        }
    )
