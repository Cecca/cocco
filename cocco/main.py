from pathlib import Path
from typing import Annotated
import markdown as md
from markupsafe import Markup
from sqlalchemy import insert, select
from cocco.avatar import generate_identicon
from cocco.evaluator import EqualityEvaluator
from cocco.judge import load_languages
from cocco.storage import Storage
from cocco.model import (
    Challenge,
    Course,
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
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
import os

from cocco.repository import (
    insert_enrollment,
    insert_submission,
    query_challenge,
    query_challenge_leaderboard,
    query_course_info,
    query_courses,
    query_sample_testcase,
    query_user_submissions,
)


# Root of the app
app = FastAPI(default_response_class=HTMLResponse)

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


def render_markdown(text: str | None) -> Markup:
    html = md.markdown(text or "", extensions=["fenced_code", "tables"])
    return Markup(html)

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


async def _populate_dummy_data(conn: AsyncConnection):
    import textwrap
    image, image_mime = generate_identicon("alice")
    user_id = (
        await conn.execute(
            insert(User)
            .values(
                email="alice@example.com",
                nickname="alice",
                image=image,
                image_mime=image_mime,
            )
            .returning(User.c.id)
        )
    ).scalar_one()

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
        if course_title == "Algorithms 101":
            await insert_enrollment(conn, user_id, course_id, UserRole.student)
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
    # the rest are hidden. Contents go on the filesystem, not in the database
    # (DESIGN.md, "Test case inputs"), so they are written through `storage`.
    cases = [
        ("5 6\n0 1 4\n0 2 1\n2 1 2\n1 3 1\n2 3 5\n3 4 3\n0 4\n", "7\n", True),
        ("4 2\n0 1 5\n2 3 7\n0 3\n", "-1\n", True),
        (
            "6 9\n0 1 7\n0 2 9\n0 5 14\n1 2 10\n1 3 15\n2 3 11\n2 5 2\n"
            "3 4 6\n5 4 9\n0 4\n",
            "20\n",
            True,
        ),
        ("2 1\n0 1 3\n1 0\n", "3\n", False),
        ("3 3\n0 1 2\n1 2 2\n0 2 10\n0 2\n", "4\n", False),
    ]
    testcase_id: int | None = None
    for order, (test_input, expected, is_sample) in enumerate(cases):
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
                )
                .returning(TestCase.c.id)
            )
        ).scalar_one()
        if testcase_id is None:
            testcase_id = tc_id

    assert testcase_id is not None

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
                    source="# seeded submission\n",
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

    await _seed_submission(user_id, "first_try.py", OutcomeEnum.runtime_error, 0.0, None)
    await _seed_submission(user_id, "route_bfs.py", OutcomeEnum.success, 0.75, 0.412)
    await _seed_submission(user_id, "shortest_route.py", OutcomeEnum.success, 1.0, 0.238)

    # A few classmates, so the leaderboard has something to rank
    for nickname, filename, elapsed_s in [
        ("bianchi", "dijkstra.py", 0.121),
        ("rossi", "heapq_solution.py", 0.164),
        ("ferrari", "route.py", 0.199),
        ("romano", "planner.py", 0.402),
    ]:
        image, image_mime = generate_identicon(nickname)
        classmate_id = (
            await conn.execute(
                insert(User)
                .values(
                    email=f"{nickname}@example.com",
                    nickname=nickname,
                    image=image,
                    image_mime=image_mime,
                )
                .returning(User.c.id)
            )
        ).scalar_one()
        await _seed_submission(
            classmate_id, filename, OutcomeEnum.success, 1.0, elapsed_s
        )




########################################################################
# dependency injection
async def get_db():
    async with engine.connect() as conn:
        yield conn

DBDep = Annotated[AsyncConnection, Depends(get_db)]

async def get_user():
    return schema.User(id=1, email="alice@example.org", nickname="alice")

CurrentUser = Annotated[schema.User, Depends(get_user)]


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
    course_id: int, challenge_id: int, request: Request, db: DBDep, user: CurrentUser
):
    if user.id is None:
        raise HTTPException(status_code=404)
    challenge = await query_challenge(db, challenge_id=challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="challenge.jinja",
        context={
            "user": user,
            "course_id": course_id,
            "challenge": challenge,
            "title": challenge["title"],
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
    if user.id is None:
        raise HTTPException(status_code=404)

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
    if user.id is None:
        raise HTTPException(status_code=404)
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
    entries = await query_challenge_leaderboard(db, challenge_id=challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-leaderboard.jinja",
        context={
            "entries": entries or [],
            "user": user,
        },
    )


@app.get("/challenges/{challenge_id}/samples/{testcase_id}/{kind}")
async def get_sample_file(challenge_id: int, testcase_id: int, kind: str, db: DBDep):
    """Serve the input or expected output of a *public* test case.

    `query_sample_testcase` returns nothing for hidden test cases, so those
    404 rather than leaking the judge's data.
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


@app.get("/users/{user_id}/avatar")
async def user_avatar(user_id: int, db: DBDep):
    row = (
        await db.execute(
            select(User.c.image, User.c.image_mime).where(User.c.id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404)
    return Response(content=row.image, media_type=row.image_mime)

