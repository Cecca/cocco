"""Exercise sets: authoring, versions, previews and the preview jobs."""

import builtins
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.exercises import format as exercise_format
from cocco.exercises.compare import describe_rules
from cocco.model import (
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseVersionStatus,
    UserRole,
)
from cocco.repository.courses import query_course
from cocco.repository.exercise_sessions import query_exercise_sessions
from cocco.repository.exercise_sets import (
    exercise_set_in_course,
    insert_exercise_job,
    insert_exercise_set,
    insert_exercise_version,
    lock_exercise_set,
    query_exercise,
    query_exercise_set,
    query_exercise_version,
    query_exercise_version_source,
    query_preview_job,
)
from cocco.repository.lifecycle import delete_exercise_set, query_course_archived_at
from cocco.web.access import CourseEditor, CurrentUser
from cocco.web.core import DBDep, _slug
from cocco.web.courses import _confirmation_matches, _danger_alert
from cocco.web.templating import templates

router = APIRouter()


# An exercise file is a few exercises typed by hand - statements, short inputs,
# a solution each - so its ceiling is a quarter of a solution file's and far
# above any real one. It is read whole and parsed on the request, which is
# fine at this size and is why the cap is checked before the read.
MAX_EXERCISE_FILE_BYTES = 256 << 10

# What a Run or a Check sends: the code in the editor and, for a Run, the text
# box's stdin. Each is stored on the job row and handed to a box, so each has
# a ceiling, and it is a script's rather than test data's.
MAX_EXERCISE_CODE_BYTES = 64 << 10


########################################################################
# Exercise sets: authoring. A set is one `.md` file, imported as an immutable
# version and compiled by the judge's build; see `cocco/exercises/format.py` for
# the file and the plan for the rest. Every route here is course staff's, keyed
# on the course in the URL with `exercise_set_in_course` as the cross-check -
# and every one is **declared above `get_challenge`**, for the course editor's
# reason: `/courses/{course_id}/{challenge_id}` would otherwise take
# `/courses/1/exercises` and refuse `exercises` as an unparseable id.


async def require_exercise_set_editor(
    set_id: int, course_id: int, db: DBDep, role: CourseEditor
) -> UserRole:
    """`require_challenge_editor` for an exercise set: course staff on the
    course in the path, and the set has to belong to it - otherwise staff of
    one course could name their own course in front of another's set. A
    mismatched pair is a set that does not exist under that course, hence 404.
    """
    if not await exercise_set_in_course(db, set_id, course_id):
        raise HTTPException(status_code=404)
    return role


ExerciseSetEditor = Annotated[UserRole, Depends(require_exercise_set_editor)]


async def _read_exercise_file(upload: UploadFile) -> tuple[str, str] | str:
    """The text of an uploaded exercise file and the title its name suggests,
    or the sentence saying why it cannot be one.

    The name is a title and nothing else - it is never a path, nothing is
    written under it - so it is the bare name the browser sent, stem only, and
    not `safe_filename`'s rewrite of it, which would turn "Loops and lists.md"
    into a title with underscores.
    """
    too_big = (
        f"That file is larger than {MAX_EXERCISE_FILE_BYTES // 1024} KB. An "
        "exercise file is a few exercises typed by hand."
    )
    # Starlette has already spooled the whole body by the time this runs, so
    # the cap saves no upload - it bounds what is read into memory and handed
    # to the parser. `size` first, which is free; the read is capped too, for
    # an upload that does not declare one.
    if upload.size is not None and upload.size > MAX_EXERCISE_FILE_BYTES:
        return too_big
    contents = await upload.read(MAX_EXERCISE_FILE_BYTES + 1)
    if len(contents) > MAX_EXERCISE_FILE_BYTES:
        return too_big
    name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    try:
        source = contents.decode("utf-8")
    except UnicodeDecodeError:
        return f"{name or 'That file'} is not valid UTF-8 text."
    return source, Path(name).stem.strip()[:200]


async def _import_exercise_file(
    db: AsyncConnection,
    user: schema.User,
    course_id: int,
    set_id: int | None,
    source: str,
    default_title: str,
) -> tuple[tuple[int, int] | None, list[schema.ExerciseProblem]]:
    """Parse a file and, if it parses, make it a version and queue its build.

    The parse is synchronous and is the import's own check: a file with an
    error in it is refused with its problems and creates nothing - no version,
    and on a first import no set. Everything the parse cannot know (a solution
    that crashes, a starter that already passes) is the build's, which parses
    the stored source again. The warnings are returned either way; the build
    finds them again and stores them on the version.

    The set (on a first import), the version and the build job are one
    transaction, committed here, with the judge's wake-up inside it - so a
    judge never wakes to a version it cannot see, and a set never exists
    without the version it was created for. Answers the set's id and the new
    version's, None when nothing was created.
    """
    parsed = exercise_format.parse(source, default_title=default_title)
    if parsed.document is None:
        return None, parsed.problems
    try:
        if set_id is None:
            set_id = await insert_exercise_set(db, course_id)
        version_id = await insert_exercise_version(
            db, set_id, source, parsed.document.title
        )
        await insert_exercise_job(
            db, kind=ExerciseJobKind.build, user_id=user.id, version_id=version_id
        )
        await db.commit()
    except IntegrityError:
        # The set was deleted between the dependency's check and this insert:
        # its foreign key refuses the version. It is gone, so this is a 404
        # like any other request for it.
        await db.rollback()
        raise HTTPException(status_code=404)
    return (set_id, version_id), parsed.problems


@router.post("/courses/{course_id}/exercises")
async def post_exercise_set_new(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    exercise_file: Annotated[UploadFile, File()],
):
    """Import a `.md` file as a new exercise set, and land on its page.

    A refusal is 200 with the alert, htmx's rule, listing every problem the
    parse found; success is `post_challenge_new`'s 204 + `HX-Redirect`, to the
    set page, where the build can be watched.
    """

    def refuse(message: str, problems=()):
        return templates.TemplateResponse(
            request=request,
            name="f-exercise-import.jinja",
            context={"message": message, "problems": list(problems)},
        )

    read = await _read_exercise_file(exercise_file)
    if isinstance(read, str):
        return refuse(read)
    imported, problems = await _import_exercise_file(db, user, course_id, None, *read)
    if imported is None:
        return refuse("The file has errors, so nothing was imported.", problems)
    set_id, _ = imported
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(
                request.url_for("get_exercise_set", course_id=course_id, set_id=set_id)
            )
        },
    )


async def _render_exercise_set_status(
    request: Request, db: AsyncConnection, course_id: int, set_id: int
):
    """The set page's status: the latest version's status and problems, and
    every version with the current one marked.

    It polls itself while any version is building and stops once none is: the
    poll is on the element, and the copy rendered after the last build has
    none, so swapping it in over itself is what stops it.
    """
    exercise_set = await query_exercise_set(db, set_id)
    if exercise_set is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-exercise-set.jinja",
        context={"course_id": course_id, "exercise_set": exercise_set, "oob": False},
    )


async def _render_exercise_set_import(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    set_id: int,
    message: str,
    ok: bool = False,
    problems=(),
):
    """A re-import's answer: the alert and the parse's problems in the form's
    own result box, and the status swapped out of band beside them.

    Two targets rather than `/admin`'s one, because the status polls itself
    every two seconds while a build runs, and an alert inside it would be
    wiped by the next poll - including a refused file's problem list, while an
    earlier version is still building. The status rides along so that an
    accepted file shows up, building, at once, and starts the poll.
    """
    exercise_set = await query_exercise_set(db, set_id)
    if exercise_set is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-exercise-import.jinja",
        context={
            "course_id": course_id,
            "exercise_set": exercise_set,
            "message": message,
            "ok": ok,
            # A refused file has no version to carry them, and a warning on an
            # accepted one is worth saying now rather than after the build.
            "problems": list(problems),
        },
    )


@router.get("/courses/{course_id}/exercises/{set_id}")
async def get_exercise_set(
    course_id: int,
    set_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSetEditor,
):
    course = await query_course(db, course_id)
    exercise_set = await query_exercise_set(db, set_id)
    if course is None or exercise_set is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="exercise-set.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "exercise_set": exercise_set,
            "exercise_sessions": await query_exercise_sessions(db, set_id=set_id),
            "user": user,
            "oob": False,
            # The open-session form's button is drawn in place on the page;
            # every later copy of the status carries it out of band.
            "first_paint": True,
        },
    )


@router.get("/f/courses/{course_id}/exercises/{set_id}")
async def get_fragment_exercise_set(
    course_id: int, set_id: int, request: Request, db: DBDep, role: ExerciseSetEditor
):
    return await _render_exercise_set_status(request, db, course_id, set_id)


@router.post("/courses/{course_id}/exercises/{set_id}/import")
async def post_exercise_set_import(
    course_id: int,
    set_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSetEditor,
    exercise_file: Annotated[UploadFile, File()],
):
    """Import a new version of the file into an existing set.

    The version becomes current only once its build is ready, so a re-import
    that fails - here, or in the build - leaves everything using the version
    that worked. Sessions do not move by themselves either way.
    """
    read = await _read_exercise_file(exercise_file)
    if isinstance(read, str):
        return await _render_exercise_set_import(request, db, course_id, set_id, read)
    imported, problems = await _import_exercise_file(
        db, user, course_id, set_id, *read
    )
    if imported is None:
        return await _render_exercise_set_import(
            request,
            db,
            course_id,
            set_id,
            "The file has errors, so no version was created.",
            problems=problems,
        )
    return await _render_exercise_set_import(
        request,
        db,
        course_id,
        set_id,
        "Imported. The judge is building the new version; it becomes current "
        "once it is ready.",
        ok=True,
        problems=problems,
    )


@router.get("/courses/{course_id}/exercises/{set_id}/versions/{version_id}/source")
async def get_exercise_version_source(
    course_id: int, set_id: int, version_id: int, db: DBDep, role: ExerciseSetEditor
):
    """A version's file exactly as it was imported, solutions and all - which
    is why it is staff's only. An attachment, so it is saved rather than
    shown; `text/markdown` with `nosniff`, so it is never run as a page on
    this origin either way."""
    version = await query_exercise_version_source(db, set_id, version_id)
    if version is None:
        raise HTTPException(status_code=404)
    filename = f"{_slug(version.title, 'exercises')}-v{version.number}.md"
    return Response(
        content=version.source,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _ready_exercise(
    db: AsyncConnection, set_id: int, version_id: int, n: int
) -> tuple[schema.ExerciseDocument, schema.Exercise]:
    """A ready version of this set and its `n`th exercise, or 404.

    Building and failed versions have no document to preview; an `n` past the
    end is an exercise that does not exist.
    """
    version = await query_exercise_version(db, set_id, version_id)
    if version is None or version.status is not ExerciseVersionStatus.ready:
        raise HTTPException(status_code=404)
    document = schema.ExerciseDocument.model_validate(version.document)
    if not 1 <= n <= len(document.exercises):
        raise HTTPException(status_code=404)
    return document, document.exercises[n - 1]


@router.get("/courses/{course_id}/exercises/{set_id}/versions/{version_id}/preview/{n}")
async def get_exercise_preview(
    course_id: int,
    set_id: int,
    version_id: int,
    n: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSetEditor,
):
    """A version's exercise, tried the way a student would try it, before any
    session is opened on it.

    Its Run and Check go through the judge as real jobs, with no session: no
    progress query reads a job without one, so nothing is recorded. It is a
    staff page, so it shows what a student never sees - the solution and the
    hidden tests' results - and says so; the student's own view is built
    elsewhere, by the one function that decides what leaves the server.
    """
    course = await query_course(db, course_id)
    exercise_set = await query_exercise_set(db, set_id)
    if course is None or exercise_set is None:
        raise HTTPException(status_code=404)
    document, exercise = await _ready_exercise(db, set_id, version_id, n)
    version = next(v for v in exercise_set["versions"] if v["id"] == version_id)
    return templates.TemplateResponse(
        request=request,
        name="exercise-preview.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "exercise_set": exercise_set,
            "version": version,
            "document": document,
            "exercise": exercise,
            "rules": describe_rules(exercise.rules),
            "n": n,
            "user": user,
        },
    )


def _marked_lines(text: str, marked) -> list[tuple[str, bool]]:
    """A program's or a test's output as lines, each with whether the
    comparison flagged it. The indexes are `compare`'s, into the output split
    on newlines; a final empty line is the trailing newline, not a line, unless
    the comparison flagged it - which `exact` does for a missing one."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "" and len(lines) - 1 not in marked:
        lines.pop()
    return [(line, i in marked) for i, line in enumerate(lines)]


def _builtin_exception_name(exception: schema.HarnessException) -> str:
    """The name a hidden test may show for an exception: the nearest of its
    classes that is one of Python's own.

    The type's own name is the student's to choose - `type(input(), (Exception,),
    {})` raises a class named after the hidden input - so printing it would
    hand the input straight back. Python's own names are a fixed list, and for
    an ordinary error (a `ValueError`, an `IndexError`) the nearest one is the
    type itself, so nothing is lost for anybody who was not trying.

    That bounds the leak; it does not close it. Which of the roughly sixty
    built-in exceptions a program raises is still its choice and can depend
    on the input - about six bits per hidden test per Check, where the
    verdict alone is already one. Accepted, since nothing here is graded: the
    plan shows a hidden test's exception type on purpose, because that is
    what the explanation beside it is about. Closing it means showing none.
    """
    for name in (exception.type, *exception.classes):
        builtin = getattr(builtins, name, None)
        if isinstance(builtin, type) and issubclass(builtin, BaseException):
            return name
    return "Exception"


def exercise_check_rows(
    result: schema.ExerciseCheckResult, exercise: schema.Exercise | None, *, redact: bool
) -> list[dict]:
    """A check's tests as the result fragment shows them, one row each.

    `exercise` is the version the check *ran against*, out of which come the
    inputs and expected outputs - None if it is gone, when only what the judge
    stored is left. With `redact` a hidden test is its verdict and nothing
    else, bar - on an error - the exception's type, which is what the
    explanation (`error_hint`) is about: `visible_outcomes`'s rule, kept in this
    function so it is testable rather than in a template. Everything else is a
    channel the program controls: its input and expected output are the test;
    what it printed and its traceback can echo the input back; its `exit` is a
    number it chose; so is its line (`exec` of a string compiled as `main.py`,
    or a forged report), and how long it took (`time.sleep(ord(c))`). A test
    the version does not have is hidden for this purpose too, since nothing
    says it is not.

    Without `redact` - staff - every test is shown in full: that is where a
    teacher finds out why a test of their own fails. `hidden` is on each row
    either way, so a page can say which ones a student sees only the verdict of.
    """
    exercise_tests = exercise.tests if exercise is not None else []
    rows = []
    for i, test_result in enumerate(result.tests):
        test = exercise_tests[i] if i < len(exercise_tests) else None
        hidden = test_result.hidden or (test is not None and test.hidden)
        row = {"n": i + 1, "verdict": test_result.verdict, "hidden": hidden}
        if redact and (hidden or test is None):
            exception = test_result.exception if test_result.verdict == "error" else None
            row["redacted"] = True
            row["exception"] = (
                None if exception is None else {"type": _builtin_exception_name(exception)}
            )
        else:
            expected = (test.expected_output or "") if test is not None else ""
            # Computed on the whole output, so an index can point past what was
            # stored of a `clipped` one; `_marked_lines` marks only lines it has.
            diff = set(test_result.diff)
            row.update(
                redacted=False,
                elapsed_s=test_result.elapsed_s,
                result=test_result,
                test=test,
                expected=_marked_lines(expected, diff),
                # An unordered diff indexes the expected output only.
                actual=_marked_lines(
                    test_result.stdout, set() if test_result.unordered else diff
                ),
            )
        rows.append(row)
    return rows


async def _render_exercise_job(
    request: Request,
    db: AsyncConnection,
    job,
    poll_url: str | None,
    message: str | None = None,
    *,
    redact: bool = False,
    link: str | None = None,
    version_id: int | None = None,
):
    """A Run's or a Check's fragment - the staff preview's and the student
    page's - polling itself at `poll_url` while the job is pending, the result
    once it is done; `exercise_check_rows` decides what a check shows, and
    `redact` is whether the viewer is a student. `version_id` is the version
    the session is on, if there is a session: see `_exercise_job_context`.

    The poll stops because the done copy replaces the polling element
    (`outerHTML`) and carries no trigger of its own. That, and not the status
    code, is what ends it: the htmx this app loads (4.x) has no special case
    for 286.
    """
    context = await _exercise_job_context(
        db, job, poll_url, message, redact=redact, version_id=version_id
    )
    # A refusal that has somewhere better to send its reader.
    context["link"] = link
    return templates.TemplateResponse(
        request=request, name="f-exercise-job.jinja", context=context
    )


async def _exercise_job_context(
    db: AsyncConnection,
    job,
    poll_url: str | None,
    message: str | None = None,
    *,
    redact: bool = False,
    version_id: int | None = None,
    exercise: schema.Exercise | None = None,
) -> dict:
    """`f-exercise-job.jinja`'s context, for `_render_exercise_job` and for a
    page that includes the fragment on its first paint.

    `version_id` is the version the session is on now, and a job that ran
    against another one is marked `earlier_version`: a switch keeps past
    results - nothing is regraded - and a page showing one under a changed
    statement should say so. `exercise` is that version's exercise when the
    caller already has it, so a check on it is not read a second time.
    """
    context = {
        "job": job,
        "poll_url": poll_url,
        "message": message,
        "link": None,
        "result": None,
        "tests": [],
        "redact": redact,
        "earlier_version": (
            job is not None and version_id is not None and job.version_id != version_id
        ),
    }
    if job is not None and job.status is ExerciseJobStatus.done:
        result = schema.EXERCISE_JOB_RESULT.validate_python(job.result)
        context["result"] = result
        if isinstance(result, schema.ExerciseCheckResult):
            if exercise is None or job.version_id != version_id:
                exercise = await query_exercise(db, job.version_id, job.exercise_index)
            context["tests"] = exercise_check_rows(result, exercise, redact=redact)
    return context


def _exercise_job_refusal(source: str, stdin: str) -> str | None:
    """Why a Run's or a Check's code or stdin cannot be queued, or None: the
    staff preview's and the student page's one check before anything is
    written."""
    for what, text in (("code", source), ("input", stdin)):
        if len(text.encode()) > MAX_EXERCISE_CODE_BYTES:
            return f"The {what} is larger than {MAX_EXERCISE_CODE_BYTES // 1024} KB."
        if "\x00" in text:
            # PostgreSQL cannot store a NUL in `text`, so it would be a 500
            # from the insert; and nothing anybody means to type is one.
            return f"The {what} contains a NUL character (\\0): remove it."
    return None


ARCHIVED_EXERCISE_JOB_REFUSAL = (
    "This course is archived. You can still read the exercises and look back "
    "at your work, but nothing new is run."
)


def _preview_job_url(request: Request, course_id: int, set_id: int, job) -> str | None:
    if job is None:
        return None
    return str(
        request.url_for(
            "get_fragment_exercise_job", course_id=course_id, set_id=set_id, job_id=job.id
        )
    )


async def _post_exercise_preview_job(
    kind: ExerciseJobKind,
    course_id: int,
    set_id: int,
    version_id: int,
    n: int,
    request: Request,
    db: AsyncConnection,
    user: schema.User,
    source: str,
    stdin: str,
):
    """Queue a preview Run or Check and answer with its polling fragment.

    `insert_exercise_job` is where pressing the button again replaces the
    queued job rather than queueing a second one. Too much code or stdin is a
    200 with the alert, htmx's rule.
    """
    await _ready_exercise(db, set_id, version_id, n)
    # An archived course runs nothing, the staff's previews included: the
    # rule `post_submission` keeps for a challenge, staff included there too.
    if await query_course_archived_at(db, course_id) is not None:
        return await _render_exercise_job(
            request, db, None, None, ARCHIVED_EXERCISE_JOB_REFUSAL
        )
    refusal = _exercise_job_refusal(source, stdin)
    if refusal is not None:
        return await _render_exercise_job(request, db, None, None, refusal)
    try:
        job_id = await insert_exercise_job(
            db,
            kind=kind,
            user_id=user.id,
            version_id=version_id,
            exercise_index=n,
            source=source,
            # The text box is a run's only; a check reads its stdin from the tests.
            stdin=stdin if kind is ExerciseJobKind.run else None,
        )
        await db.commit()
    except IntegrityError:
        # The set was deleted since `_ready_exercise` read it: the version's
        # foreign key refuses the job. Gone, so 404.
        await db.rollback()
        raise HTTPException(status_code=404)
    job = await query_preview_job(db, set_id, job_id, user.id)
    return await _render_exercise_job(
        request, db, job, _preview_job_url(request, course_id, set_id, job)
    )


@router.post("/courses/{course_id}/exercises/{set_id}/versions/{version_id}/preview/{n}/run")
async def post_exercise_preview_run(
    course_id: int,
    set_id: int,
    version_id: int,
    n: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSetEditor,
    source: Annotated[str, Form()] = "",
    stdin: Annotated[str, Form()] = "",
):
    return await _post_exercise_preview_job(
        ExerciseJobKind.run, course_id, set_id, version_id, n,
        request, db, user, source, stdin,
    )


@router.post("/courses/{course_id}/exercises/{set_id}/versions/{version_id}/preview/{n}/check")
async def post_exercise_preview_check(
    course_id: int,
    set_id: int,
    version_id: int,
    n: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSetEditor,
    source: Annotated[str, Form()] = "",
):
    return await _post_exercise_preview_job(
        ExerciseJobKind.check, course_id, set_id, version_id, n,
        request, db, user, source, "",
    )


@router.get("/f/courses/{course_id}/exercises/{set_id}/jobs/{job_id}")
async def get_fragment_exercise_job(
    course_id: int,
    set_id: int,
    job_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSetEditor,
):
    """A preview job, polled every half second until it is done. Only the
    asker's own preview of this set: anything else is not found, see
    `query_preview_job`."""
    job = await query_preview_job(db, set_id, job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404)
    return await _render_exercise_job(
        request, db, job, _preview_job_url(request, course_id, set_id, job)
    )


@router.post("/courses/{course_id}/exercises/{set_id}/delete")
async def post_exercise_set_delete(
    course_id: int,
    set_id: int,
    request: Request,
    db: DBDep,
    role: ExerciseSetEditor,
    confirm: Annotated[str, Form()] = "",
):
    """Delete a set that no session was ever opened on: its versions and every
    build and preview job on them. A set with sessions is refused rather than
    cascaded - those sessions are a class's work, and deleting a session is
    its own, deliberate act.

    Asks for the set's title typed out, the danger zones' rule, and lands on
    the course page, which is the list the set has just left.
    """
    exercise_set = await query_exercise_set(db, set_id)
    if exercise_set is None:
        raise HTTPException(status_code=404)
    if not _confirmation_matches(confirm, exercise_set["title"]):
        return _danger_alert(
            request,
            "Type the set's title exactly as it is shown to delete it. "
            "Nothing was changed.",
        )
    n_sessions = await lock_exercise_set(db, set_id)
    if n_sessions is None:
        raise HTTPException(status_code=404)
    if n_sessions:
        await db.rollback()
        return _danger_alert(
            request,
            f"This set has {n_sessions} session{'' if n_sessions == 1 else 's'}, "
            "so it cannot be deleted. Nothing was changed.",
        )
    await delete_exercise_set(db, set_id)
    await db.commit()
    return Response(
        status_code=204,
        headers={"HX-Redirect": str(request.url_for("get_course", course_id=course_id))},
    )
