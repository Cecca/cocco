"""The student's side of an exercise session: the strip, the exercise page, Run and Check."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.exercises.compare import describe_rules
from cocco.model import ExerciseJobKind, UserRole
from cocco.repository.courses import query_course
from cocco.repository.exercise_sessions import (
    query_exercise_session_state,
    query_last_session_jobs,
    query_session_heading,
    query_session_job,
    query_session_strip,
)
from cocco.repository.exercise_sets import insert_exercise_job, query_exercise
from cocco.repository.lifecycle import query_course_archived_at
from cocco.web.access import CurrentUser
from cocco.web.core import DBDep, EDITOR_ROLES
from cocco.web.exercise_sessions import (
    ExerciseLocked,
    ExerciseSessionMember,
    _exercise_status,
    _n_visible,
    _register_participant,
    _session_exercise_in_range,
    _session_job_gate,
    exercise_lock_refusal,
    require_session_exercise,
)
from cocco.web.exercise_sets import (
    ARCHIVED_EXERCISE_JOB_REFUSAL,
    _exercise_job_context,
    _exercise_job_refusal,
    _render_exercise_job,
)
from cocco.web.templating import templates

router = APIRouter()


########################################################################
# The student page: one exercise of a session, with the strip of all of them,
# the statement and the editor; Run and Check are judge jobs. Members of the
# course, through `require_session_exercise` and `session_job_refusal` - which
# are the whole of pacing - and, like every session route, **declared above
# `get_challenge`**. The exercise is `{n:int}`, with the convertor: a plain
# `{n}` would also match the staff's `.../dashboard` and `.../projector` and
# 422 on them, since a route does not fall through on a failed validation.


def student_exercise_view(exercise: schema.Exercise, revealed: bool) -> dict:
    """One exercise as a student may see it, out of the compiled document:
    the statement, the starter, the hints, the comparison rules and time limit,
    the visible tests with their expected outputs, how many tests are hidden -
    and the solution once it has been revealed in this session, not before.

    `visible_outcomes`'s rule, kept in a function rather than in a template:
    the page is handed this and never the document, so a hidden test's input
    and expected output and an unrevealed solution never leave the server.
    Hints are not secret - they are revealed one at a time in the browser.
    """
    visible = [test for test in exercise.tests if not test.hidden]
    return {
        "title": exercise.title,
        "statement": exercise.statement,
        "starter": exercise.starter,
        "hints": list(exercise.hints),
        "rules": describe_rules(exercise.rules),
        "time_s": exercise.time_s,
        "tests": [
            {
                "input": test.input,
                "expected_output": test.expected_output,
                "expected_error": test.expected_error,
            }
            for test in visible
        ],
        "n_hidden": len(exercise.tests) - len(visible),
        "solution": exercise.solution if revealed else None,
    }


def session_strip_view(strip: dict, role: UserRole) -> dict:
    """The exercise strip as this person may see it, out of
    `query_session_strip`: one entry per exercise they may open, with its
    title and their status on it, and a count of the locked ones after - whose
    titles stay on the server, `student_session_view`'s rule. Staff pass the
    pacing here as at the gate.

    Status is derived, never stored: `solved` once any check passed, and it
    stays solved; `attempted` with checks and no pass; `not_attempted` with
    none. `checking` - a check queued or running - is beside it, not instead.
    """
    titles = strip["titles"]
    visible = (
        len(titles)
        if role in EDITOR_ROLES
        else _n_visible(strip["mode"], strip["unlocked_through"], len(titles))
    )
    checks = {check["n"]: check for check in strip["checks"]}
    exercises = [
        {"n": n, "title": title, **_exercise_status(checks.get(n))}
        for n, title in enumerate(titles[:visible], start=1)
    ]
    return {"exercises": exercises, "n_locked": len(titles) - visible}


async def _session_strip_context(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    session_id: int,
    user: schema.User,
    role: UserRole,
    n: int,
    wait: bool,
) -> dict:
    """`f-exercise-strip.jinja`'s context: the strip, the exercise the page is
    on, and the URL it polls - which carries both, so the poll keeps the
    highlight and a wait page keeps waiting."""
    strip = await query_session_strip(db, session_id, user.id)
    if strip is None:
        raise HTTPException(status_code=404)
    poll_url = request.url_for(
        "get_fragment_session_strip", course_id=course_id, session_id=session_id
    ).include_query_params(n=n, **({"wait": "1"} if wait else {}))
    return {
        "course_id": course_id,
        "session_id": session_id,
        "strip": session_strip_view(strip, role),
        "n": n,
        "strip_url": str(poll_url),
        # A closed session's pacing is final, but not its checks: closing it
        # cancels none, so one queued before the close is polled for until it
        # is done.
        "polls": strip["closed_at"] is None
        or any(check["checking"] for check in strip["checks"]),
    }


STAFF_SESSION_JOB_REFUSAL = (
    "You are staff on this course, so Run and Check here are not recorded in "
    "the session - its jobs are the students' work. Try the exercise in the "
    "set's preview, which runs it the same way and records nothing."
)


def _session_preview_url(request: Request, course_id: int, state, n: int) -> str:
    """The staff preview of exercise `n` of the version a session is on."""
    return str(
        request.url_for(
            "get_exercise_preview",
            course_id=course_id,
            set_id=state.set_id,
            version_id=state.version_id,
            n=n,
        )
    )


def _session_job_url(request: Request, course_id: int, session_id: int, job) -> str | None:
    if job is None:
        return None
    return str(
        request.url_for(
            "get_fragment_session_job",
            course_id=course_id,
            session_id=session_id,
            job_id=job.id,
        )
    )


@router.get("/courses/{course_id}/sessions/{session_id}/{n:int}")
async def get_session_exercise(
    course_id: int,
    session_id: int,
    n: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionMember,
):
    """Exercise `n` of a session, as a student works on it.

    Behind `require_session_exercise`, called here rather than declared as a
    dependency so that its pacing refusal can be caught: an exercise the
    teacher has not reached is the "wait for the teacher" page, whose strip
    polls with `wait` and is answered with a redirect back here once it
    unlocks. That page has no editor, so nothing is lost by the reload; the
    exercise page itself never reloads, and its poll swaps only the strip.

    The editor opens on the browser's draft when it has one (`exercise.js`)
    and otherwise on the newest source this person sent from this exercise -
    every Run and Check carries one - or the starter. Each tab shows its last
    result. A closed session shows all of it, with no buttons.
    """
    course = await query_course(db, course_id)
    session = await query_session_heading(db, session_id)
    if course is None or session is None:
        raise HTTPException(status_code=404)
    context = {
        "course": course,
        "course_id": course_id,
        "session_id": session_id,
        "label": session["label"],
        "n": n,
        "user": user,
    }
    try:
        access = await require_session_exercise(session_id, n, db, role)
    except ExerciseLocked as locked:
        await _register_participant(db, session_id, user, role)
        context.update(
            await _session_strip_context(
                request, db, course_id, session_id, user, role, n, wait=locked.waiting
            ),
            message=locked.detail,
            waiting=locked.waiting,
        )
        return templates.TemplateResponse(
            request=request, name="exercise-wait.jinja", context=context
        )
    exercise = await query_exercise(db, access.state.version_id, n)
    if exercise is None:
        raise HTTPException(status_code=404)
    await _register_participant(db, session_id, user, role)
    staff = role in EDITOR_ROLES
    last = await query_last_session_jobs(db, session_id, user.id, n)
    # Written last, not created last: see `query_last_session_jobs`.
    newest = max(last.values(), key=lambda job: job.submitted_at, default=None)
    view = student_exercise_view(exercise, revealed=n in session["revealed"])
    first_visible = view["tests"][0]["input"] if view["tests"] else ""
    context.update(
        await _session_strip_context(
            request, db, course_id, session_id, user, role, n, wait=False
        ),
        exercise=view,
        n_exercises=access.state.n_exercises,
        closed=access.state.closed_at is not None,
        staff=staff,
        preview_url=_session_preview_url(request, course_id, access.state, n) if staff else None,
        source=newest.source if newest is not None else view["starter"],
        stdin=last["run"].stdin if "run" in last else first_visible,
        # Keyed by the person too: a lab machine is shared, and the next
        # student to sit at it must not open on somebody else's code.
        draft_key=f"cocco:exercise:{user.id}:{session_id}:{n}",
        check=await _exercise_job_context(
            db,
            last.get("check"),
            _session_job_url(request, course_id, session_id, last.get("check")),
            redact=not staff,
            version_id=access.state.version_id,
            exercise=exercise,
        ),
        run=await _exercise_job_context(
            db,
            last.get("run"),
            _session_job_url(request, course_id, session_id, last.get("run")),
            redact=not staff,
            version_id=access.state.version_id,
        ),
    )
    return templates.TemplateResponse(request=request, name="exercise.jinja", context=context)


@router.get("/f/courses/{course_id}/sessions/{session_id}/strip")
async def get_fragment_session_strip(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionMember,
    n: int = 0,
    wait: bool = False,
):
    """The strip, polled every three seconds: what changes is the pacing and
    the asker's own checks, and swapping this tiny fragment is all the poll
    does - never the editor.

    With `wait`, the page polling is "wait for the teacher" on exercise `n`,
    and once `n` opens - or the session closes, and there is nothing left to
    wait for - the answer is `HX-Redirect` to it, which no editor is lost to.
    An `n` the session no longer has (a switch to a shorter version) goes to
    the session's landing instead.
    """
    if wait:
        state = await query_exercise_session_state(db, session_id)
        if state is None:
            raise HTTPException(status_code=404)
        target = None
        if not 1 <= n <= state.n_exercises:
            target = request.url_for(
                "get_exercise_session", course_id=course_id, session_id=session_id
            )
        elif (
            state.closed_at is not None
            or exercise_lock_refusal(state, n, role, attempt=False) is None
        ):
            target = request.url_for(
                "get_session_exercise", course_id=course_id, session_id=session_id, n=n
            )
        if target is not None:
            return Response(status_code=204, headers={"HX-Redirect": str(target)})
    context = await _session_strip_context(
        request, db, course_id, session_id, user, role, n, wait
    )
    return templates.TemplateResponse(
        request=request, name="f-exercise-strip.jinja", context=context
    )


async def _post_session_job(
    kind: ExerciseJobKind,
    course_id: int,
    session_id: int,
    n: int,
    request: Request,
    db: AsyncConnection,
    user: schema.User,
    role: UserRole,
    source: str,
    stdin: str,
):
    """Queue a Run or a Check of exercise `n` and answer with its polling
    fragment; a refusal is a 200 with the alert, htmx's rule.

    `_session_job_gate` - the pacing and closed-ness - is asked inside the
    transaction that inserts the job, and holds the session's row until it
    commits, so a close or a pacing move landing meanwhile waits for it rather
    than letting it into a session it no longer belongs to. The job runs
    against the version the session is on under that lock. The code is the
    server's copy of the draft, which is what the page falls back on.

    Course staff - administrators included - are refused, and pointed at the
    set's preview, which runs the same jobs and records nothing: a session's
    jobs are its students' work, so its progress needs no question about who
    is staff. They can still open the page, past the pacing, to see it.

    An archived course takes no job at all: this is the third of archiving's
    enforcement points, beside `post_submission` and `post_course_enroll`.
    Asked first, since it is the answer for staff and students alike.
    """
    if await query_course_archived_at(db, course_id) is not None:
        return await _render_exercise_job(
            request, db, None, None, ARCHIVED_EXERCISE_JOB_REFUSAL
        )
    if role in EDITOR_ROLES:
        state = await query_exercise_session_state(db, session_id)
        if state is None:
            raise HTTPException(status_code=404)
        # Before the link is built: there is no preview of an exercise the
        # version does not have.
        _session_exercise_in_range(state, n)
        return await _render_exercise_job(
            request, db, None, None, STAFF_SESSION_JOB_REFUSAL,
            link=_session_preview_url(request, course_id, state, n),
        )
    refusal = _exercise_job_refusal(source, stdin)
    if refusal is not None:
        return await _render_exercise_job(request, db, None, None, refusal)
    state, refusal = await _session_job_gate(db, session_id, n, role)
    if refusal is not None:
        await db.rollback()
        return await _render_exercise_job(request, db, None, None, refusal)
    job_id = await insert_exercise_job(
        db,
        kind=kind,
        user_id=user.id,
        version_id=state.version_id,
        session_id=session_id,
        exercise_index=n,
        source=source,
        stdin=stdin if kind is ExerciseJobKind.run else None,
    )
    await db.commit()
    job = await query_session_job(db, session_id, job_id, user.id)
    return await _render_exercise_job(
        request,
        db,
        job,
        _session_job_url(request, course_id, session_id, job),
        redact=True,
        version_id=state.version_id,
    )


@router.post("/courses/{course_id}/sessions/{session_id}/{n:int}/run")
async def post_session_run(
    course_id: int,
    session_id: int,
    n: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionMember,
    source: Annotated[str, Form()] = "",
    stdin: Annotated[str, Form()] = "",
):
    return await _post_session_job(
        ExerciseJobKind.run, course_id, session_id, n,
        request, db, user, role, source, stdin,
    )


@router.post("/courses/{course_id}/sessions/{session_id}/{n:int}/check")
async def post_session_check(
    course_id: int,
    session_id: int,
    n: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionMember,
    source: Annotated[str, Form()] = "",
):
    return await _post_session_job(
        ExerciseJobKind.check, course_id, session_id, n,
        request, db, user, role, source, "",
    )


@router.get("/f/courses/{course_id}/sessions/{session_id}/jobs/{job_id}")
async def get_fragment_session_job(
    course_id: int,
    session_id: int,
    job_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionMember,
):
    """A Run or a Check of a session, polled every half second until it is
    done. The asker's own and nobody else's, staff included - the dashboard is
    where staff read a student's work - and redacted for a student by
    `exercise_check_rows`."""
    job = await query_session_job(db, session_id, job_id, user.id)
    state = await query_exercise_session_state(db, session_id)
    if job is None or state is None:
        raise HTTPException(status_code=404)
    return await _render_exercise_job(
        request,
        db,
        job,
        _session_job_url(request, course_id, session_id, job),
        redact=role not in EDITOR_ROLES,
        version_id=state.version_id,
    )
