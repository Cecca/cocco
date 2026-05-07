from typing import Annotated
import markdown as md
from markupsafe import Markup
from sqlalchemy import insert, select
from cocco.avatar import generate_identicon
from cocco.evaluator import EqualityEvaluator
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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
import os

from cocco.repository import query_course_info, query_courses, insert_enrollment, query_challenge, query_user_submissions


# Root of the app
app = FastAPI(default_response_class=HTMLResponse)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# The database
url = f"postgresql+psycopg://{os.environ['PGUSER']}@/{os.environ['PGDATABASE']}?host={os.environ['PGHOST']}"
engine = create_async_engine(url)

# The templates directory
templates = Jinja2Templates(directory="templates")


def render_markdown(text: str | None) -> Markup:
    html = md.markdown(text or "", extensions=["fenced_code", "tables"])
    return Markup(html)

def render_datetime(value):
    from babel.dates import format_datetime
    from datetime import datetime
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return format_datetime(value, locale="en")

templates.env.filters["markdown"] = render_markdown
templates.env.filters["datetime"] = render_datetime


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
            challenge_id = (
                await conn.execute(
                    insert(Challenge)
                    .values(
                        title=f"{course_title} - Challenge {n + 1}",
                        description=f"challenge {n + 1} of {course_title}",
                        course_id=course_id,
                        max_memory_bytes=1 << 20,
                        max_time_s=1.0,
                        max_threads=1,
                        evaluator=evaluator,
                    )
                    .returning(Challenge.c.id)
                )
            ).scalar_one()
            if first_challenge_id is None:
                first_challenge_id = challenge_id

    assert first_challenge_id is not None
    testcase_id = (
        await conn.execute(
            insert(TestCase)
            .values(
                challenge_id=first_challenge_id,
                execution_order=0,
                input_path="dummy/in_0.txt",
                expected_output_path="dummy/out_0.txt",
            )
            .returning(TestCase.c.id)
        )
    ).scalar_one()

    failed_submission_id = (
        await conn.execute(
            insert(Submission)
            .values(
                user_id=user_id,
                challenge_id=first_challenge_id,
                status=SubmissionStatus.done,
                language="python",
                source="raise RuntimeError('boom')",
            )
            .returning(Submission.c.id)
        )
    ).scalar_one()
    await conn.execute(
        insert(TestOutcome).values(
            submission_id=failed_submission_id,
            testcase_id=testcase_id,
            outcome=OutcomeEnum.runtime_error,
            score=0.0,
            message="runtime error",
            stdout="",
            stderr="RuntimeError: boom",
        )
    )

    passing_submission_id = (
        await conn.execute(
            insert(Submission)
            .values(
                user_id=user_id,
                challenge_id=first_challenge_id,
                status=SubmissionStatus.done,
                language="python",
                source="print('hi')",
            )
            .returning(Submission.c.id)
        )
    ).scalar_one()
    await conn.execute(
        insert(TestOutcome).values(
            submission_id=passing_submission_id,
            testcase_id=testcase_id,
            outcome=OutcomeEnum.success,
            score=1.0,
            message="ok",
            stdout="hi\n",
            stderr="",
        )
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
    await create_db_and_tables(dummy=True)


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
    challenge = await query_challenge(db, user_id=user.id, challenge_id=challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="challenge.jinja",
        context={
            "user": user,
            "course_id": course_id,
            "challenge": challenge
        },
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
            "submissions": submissions
        },
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

