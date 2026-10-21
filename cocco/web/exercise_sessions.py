"""Exercise sessions, the teacher's side: gates, controls, pacing, dashboard and projector."""

from collections import Counter
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.model import ExerciseJobStatus, ExerciseSessionMode, UserRole
from cocco.repository.courses import query_course
from cocco.repository.exercise_sessions import (
    ExerciseSessionState,
    close_exercise_session,
    exercise_session_in_course,
    insert_exercise_session,
    insert_session_participant,
    lock_exercise_set_for_session,
    open_exercise_session_to_all,
    pace_exercise_session,
    query_exercise_session,
    query_exercise_session_state,
    query_last_session_jobs,
    query_max_checked_exercise,
    query_session_cohort_member,
    query_session_counts,
    query_session_progress,
    query_set_current_version,
    query_version_exercise_count,
    reveal_exercise_solution,
    switch_exercise_session_version,
)
from cocco.repository.exercise_sets import query_exercise
from cocco.repository.lifecycle import delete_exercise_session
from cocco.web.access import CourseEditor, CourseMember, CurrentUser, Forbidden
from cocco.web.core import DBDep, EDITOR_ROLES
from cocco.web.courses import _confirmation_matches, _danger_alert
from cocco.web.exercise_sets import ExerciseSetEditor, _exercise_job_context
from cocco.web.templating import templates

router = APIRouter()


########################################################################
# Exercise sessions: one run of a set with one group of students, with its own
# pacing. Staff open one from the set page and drive it from its own page;
# the class finds the open ones on the course page. Keyed on the course in the
# URL like the sets, with `exercise_session_in_course` as the cross-check, and
# **declared above `get_challenge`** for the same reason.


async def require_exercise_session_member(
    session_id: int, course_id: int, db: DBDep, role: CourseMember
) -> UserRole:
    """`require_challenge_viewer` for a session: anyone enrolled on the course
    in the path, and the session has to belong to it - otherwise a student of
    one course could name their own course in front of another's session. A
    mismatched pair is a session that does not exist under that course: 404,
    asked after the membership gate so a stranger learns nothing either way."""
    if not await exercise_session_in_course(db, session_id, course_id):
        raise HTTPException(status_code=404)
    return role


ExerciseSessionMember = Annotated[UserRole, Depends(require_exercise_session_member)]


async def require_exercise_session_editor(
    session_id: int, course_id: int, db: DBDep, role: CourseEditor
) -> UserRole:
    """The same gate for course staff: opening, pacing, revealing and
    switching a session's version are theirs."""
    if not await exercise_session_in_course(db, session_id, course_id):
        raise HTTPException(status_code=404)
    return role


ExerciseSessionEditor = Annotated[UserRole, Depends(require_exercise_session_editor)]


def exercise_lock_refusal(state, n: int, role: UserRole, *, attempt: bool) -> str | None:
    """Why this person may not have exercise `n` of a session right now, or
    None. `state` is `query_exercise_session_state`'s row; `n` is already
    known to be one of its version's exercises.

    The whole of pacing, in one function the read gate and the job gate both
    call, the way `require_challenge_visible` is the whole of hiding:

    - a **closed** session refuses every new job (`attempt`), staff's
      included, and still lets everybody read - "students can still review";
    - a **live** session refuses a student anything past `unlocked_through`,
      reading included: the statement of an exercise the teacher has not
      reached is not out yet. An `open` session has nothing locked.

    Course staff pass the pacing, as they pass a hidden challenge: it would be
    a strange lock that kept the teacher out of the exercise they are about to
    unlock.
    """
    if attempt and state.closed_at is not None:
        return "This session is closed. You can review your work, but nothing new is accepted."
    if role in EDITOR_ROLES:
        return None
    if state.mode is ExerciseSessionMode.live and n > state.unlocked_through:
        return (
            f"Exercise {n} is not unlocked yet. Your teacher unlocks the "
            "exercises one at a time - wait for them to move the class on."
        )
    return None


def _session_exercise_in_range(state, n: int) -> None:
    """404 unless `n` is one of the session's version's exercises: an `n`
    past the end is an exercise that does not exist, whatever the pacing."""
    if not 1 <= n <= (state.n_exercises or 0):
        raise HTTPException(status_code=404)


class ExerciseLocked(Forbidden):
    """`require_session_exercise`'s refusal of an exercise the pacing has not
    reached. A `Forbidden`, so anywhere else it is the 403 page; the student
    page catches it to say "wait for the teacher" instead, and to wait -
    `waiting` is whether there is anything to wait for, which in a closed
    session there is not."""

    def __init__(self, detail: str, waiting: bool):
        super().__init__(detail)
        self.waiting = waiting


@dataclass(frozen=True)
class SessionExercise:
    """What `require_session_exercise` answers: the session's state as the
    gate read it, the exercise's position, and the asker's role on the
    course."""

    state: object
    n: int
    role: UserRole


async def require_session_exercise(
    session_id: int, n: int, db: DBDep, role: ExerciseSessionMember
) -> SessionExercise:
    """The gate on reading exercise `n` of a session: membership and the
    cross-check through `ExerciseSessionMember`, then a 404 for an exercise
    the version does not have, then the pacing.

    A locked exercise is a `Forbidden` page with a sentence and not a 404,
    the scheduled challenge's rule: the session is on the student's course
    page, and a 404 at the next exercise of it would read as a broken link.

    The route it guards is to be declared as `.../sessions/{session_id}/{n:int}`,
    with the convertor: a plain `{n}` would also match `.../dashboard` and
    `.../projector` and 422 on them, since a route does not fall through to
    the next one on a failed validation.
    """
    state = await query_exercise_session_state(db, session_id)
    if state is None:
        raise HTTPException(status_code=404)
    _session_exercise_in_range(state, n)
    refusal = exercise_lock_refusal(state, n, role, attempt=False)
    if refusal is not None:
        raise ExerciseLocked(refusal, waiting=state.closed_at is None)
    return SessionExercise(state=state, n=n, role=role)


SessionExerciseReader = Annotated[SessionExercise, Depends(require_session_exercise)]


async def session_job_refusal(
    db: AsyncConnection, session_id: int, n: int, role: UserRole
) -> str | None:
    """The job gate: why a Run or a Check of exercise `n` is refused, or None.
    For the route that queues one, *inside the transaction that inserts it*.

    The session row is read `FOR SHARE` and held until that transaction
    ends, which is what makes the answer still true when the job lands: a
    close or a pacing move is an `UPDATE` of the row and waits for the job to
    commit, and so does a version switch, which would otherwise count the
    session's checks before this one exists. A sentence rather than a
    `Forbidden`, because the caller is an htmx form and its refusal is a 200
    with an alert; an exercise the version does not have is still a 404.
    """
    return (await _session_job_gate(db, session_id, n, role))[1]


async def _session_job_gate(
    db: AsyncConnection, session_id: int, n: int, role: UserRole
) -> tuple[ExerciseSessionState, str | None]:
    """`session_job_refusal`, with the state it was decided on: the version
    a job lets in is the one the session is on *under that lock*, and a
    switch cannot move it before the job commits."""
    state = await query_exercise_session_state(db, session_id, lock="share")
    if state is None:
        raise HTTPException(status_code=404)
    _session_exercise_in_range(state, n)
    return state, exercise_lock_refusal(state, n, role, attempt=True)


def _version_switch(session) -> dict | None:
    """What "switch to the latest version" would do to a session, from
    `query_exercise_session`'s document: None when the session is already on
    the set's current version.

    Exercises are matched by position, so the confirmation lists every one
    whose title changed - a reordered file is the one mistake position cannot
    catch - and what was appended or dropped. `refusal` is set when the new
    version has fewer exercises than the session has checks for.
    """
    version, latest = session["version"], session["latest"]
    if latest is None or latest["id"] == version["id"]:
        return None
    old, new = version["titles"], latest["titles"]
    shared = min(len(old), len(new))
    max_checked = session["max_checked"]
    refusal = None
    if max_checked is not None and max_checked > len(new):
        refusal = _switch_refusal(latest["number"], len(new), max_checked, old[max_checked - 1])
    return {
        "latest": latest,
        "refusal": refusal,
        "renamed": [(i + 1, old[i], new[i]) for i in range(shared) if old[i] != new[i]],
        "added": [(i + 1, new[i]) for i in range(shared, len(new))],
        "dropped": [(i + 1, old[i]) for i in range(shared, len(old))],
    }


def _switch_refusal(number: int, n_new: int, max_checked: int, title: str) -> str:
    """The switch's refusal, spelled once for the page and the route."""
    return (
        f"v{number} has {n_new} exercise{'' if n_new == 1 else 's'}, and this "
        f"session has checks on exercise {max_checked}, “{title}”. Exercises are "
        "matched by position, so those checks would have nothing to belong to. "
        "Add the exercise back to the file and import it again."
    )


async def _render_exercise_session_controls(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    session_id: int,
    message: str | None = None,
    ok: bool = False,
    view: str = "panel",
):
    """The session's control panel: one swap target, alert folded in, which
    every staff route on the session answers with - `/admin`'s rule.

    Not polled: a second teacher's page can go stale, which is why every
    pacing button carries what its page showed and the write is a
    compare-and-set on it (`pace_exercise_session`).

    `view` is the page the form was on. The projector carries the pacing and
    the reveal buttons too, inside its own polled fragment, and a press there
    is answered with that fragment rather than with a panel it does not have.
    """
    if view == "projector":
        return await _render_exercise_projector(request, db, course_id, session_id, message, ok)
    session = await query_exercise_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-exercise-session.jinja",
        context={
            "course_id": course_id,
            "session": session,
            "switch": _version_switch(session),
            "message": message,
            "ok": ok,
        },
    )


@router.post("/courses/{course_id}/exercises/{set_id}/sessions")
async def post_exercise_session_new(
    course_id: int,
    set_id: int,
    request: Request,
    db: DBDep,
    role: ExerciseSetEditor,
    label: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "",
):
    """Open a session on the set's current version, and land on its page.

    A refusal is 200 with the alert; success is 204 + `HX-Redirect`, as a new
    challenge is. Defaulted form fields, `post_administrator`'s reason: a
    missing one would be a 422 whose body htmx drops.

    The set is locked (`lock_exercise_set_for_session`) while the session is
    inserted: a delete counts the set's sessions under the same lock, so it
    either sees this one or this one waits and finds the set gone.
    """

    def refuse(sentence: str):
        return _danger_alert(request, sentence)

    label = label.strip()
    if not label:
        return refuse("Give the session a label, such as “Lab A — 20 Oct”.")
    if len(label) > 200:
        return refuse("That label is longer than 200 characters.")
    try:
        session_mode = ExerciseSessionMode(mode)
    except ValueError:
        return refuse("Choose whether the session is live or open.")
    exercise_set = await lock_exercise_set_for_session(db, set_id)
    if exercise_set is None:
        await db.rollback()
        raise HTTPException(status_code=404)
    if exercise_set.archived_at is not None:
        await db.rollback()
        return refuse("This set is archived, so no session can be opened on it.")
    if exercise_set.current_version_id is None:
        await db.rollback()
        return refuse(
            "No version of this set has built yet, so there is nothing to open "
            "a session on."
        )
    session_id = await insert_exercise_session(
        db,
        set_id,
        exercise_set.current_version_id,
        label,
        session_mode,
        # A live session starts on its first exercise; an open one has no
        # pacing, and is stored fully unlocked so the number never lies.
        1 if session_mode is ExerciseSessionMode.live else exercise_set.n_exercises,
    )
    await db.commit()
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(
                request.url_for(
                    "get_exercise_session", course_id=course_id, session_id=session_id
                )
            )
        },
    )


@router.get("/courses/{course_id}/sessions/{session_id}")
async def get_exercise_session(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionMember,
):
    """One URL, two pages, as a challenge is: course staff get the control
    panel, everybody else the session's landing - its exercises as far as the
    pacing lets them see. The student's exercise page itself is per exercise,
    under this URL."""
    course = await query_course(db, course_id)
    session = await query_exercise_session(db, session_id)
    if course is None or session is None:
        raise HTTPException(status_code=404)
    staff = role in EDITOR_ROLES
    context = {
        "course": course,
        "course_id": course_id,
        "session": session,
        "user": user,
    }
    if staff:
        context.update(switch=_version_switch(session), message=None, ok=False)
        name = "exercise-session-staff.jinja"
    else:
        await _register_participant(db, session_id, user, role)
        context["session"] = student_session_view(session)
        context["session_id"] = session_id
        name = "exercise-session.jinja"
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.post("/courses/{course_id}/sessions/{session_id}/delete")
async def post_exercise_session_delete(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    role: ExerciseSessionEditor,
    confirm: Annotated[str, Form()] = "",
):
    """Delete a session with everything in it: its students' Runs and Checks,
    its participants and its reveals. The set and its versions stay, and so
    do the set's other sessions.

    Asks for the session's label typed out, the danger zones' rule, and lands
    on the set page, the list the session has just left. Allowed whatever the
    session's state, live included, and while the judge is running one of its
    jobs: a result landing on a deleted job updates zero rows.
    """
    session = await query_exercise_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404)
    if not _confirmation_matches(confirm, session["label"]):
        return _danger_alert(
            request,
            "Type the session's label exactly as it is shown to delete it. "
            "Nothing was changed.",
        )
    # `FOR UPDATE`, so a job, a participant or a reveal being written waits
    # for the delete, or the delete for it; see `delete_exercise_session`.
    state = await query_exercise_session_state(db, session_id, lock="update")
    if state is None:
        await db.rollback()
        raise HTTPException(status_code=404)
    await delete_exercise_session(db, session_id)
    await db.commit()
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(
                request.url_for("get_exercise_set", course_id=course_id, set_id=state.set_id)
            )
        },
    )


async def _register_participant(
    db: AsyncConnection, session_id: int, user: schema.User, role: UserRole
) -> None:
    """A student's visit to a session - its landing or any exercise page of
    it - makes them one of its participants, the dashboard's denominator: the
    students who came, not everybody enrolled. Course staff are never
    participants, administrators included: they come to run the session, and
    counting them would put the teacher among the students who have not tried.
    A GET that writes, as `touch_api_token` makes one: committed here, and
    harmless to repeat (`insert_session_participant`)."""
    if role in EDITOR_ROLES:
        return
    try:
        await insert_session_participant(db, session_id, user.id)
        await db.commit()
    except IntegrityError:
        # The session was deleted since the gate found it; the page that
        # follows finds it gone too.
        await db.rollback()


def _n_visible(mode: str, unlocked_through: int, n_exercises: int) -> int:
    """How many of a session's exercises a student may see: all of them in
    an open session, the first `unlocked_through` in a live one -
    `exercise_lock_refusal`'s rule, for a page listing them rather than a gate
    asking about one."""
    if mode == ExerciseSessionMode.open.value:
        return n_exercises
    return min(unlocked_through, n_exercises)


def student_session_view(session) -> dict:
    """A session as a student may see it, out of `query_exercise_session`'s
    staff document: what the landing page shows and nothing else.

    `visible_outcomes`'s rule, kept in a function rather than in a template:
    the titles past the pacing stay on the server with the statements they
    head, and so do the other version, the reveals and who checked what.
    """
    titles = session["version"]["titles"]
    visible = _n_visible(session["mode"], session["unlocked_through"], len(titles))
    return {
        "label": session["label"],
        "mode": session["mode"],
        "closed_at": session["closed_at"],
        "title": session["version"]["title"],
        "titles": titles[:visible],
        "n_locked": len(titles) - visible,
    }


@router.post("/courses/{course_id}/sessions/{session_id}/pace")
async def post_exercise_session_pace(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    role: ExerciseSessionEditor,
    from_: Annotated[int, Form(alias="from")] = -1,
    to: Annotated[int, Form()] = -1,
    view: Annotated[str, Form()] = "panel",
):
    """Next, previous and unlock all: one route, since each is a move from
    the number the page showed to another. See `pace_exercise_session` for why
    it is a compare-and-set."""
    moved = await pace_exercise_session(db, session_id, from_, to)
    await db.commit()
    if moved:
        return await _render_exercise_session_controls(
            request, db, course_id, session_id, view=view
        )
    # Say what actually stopped it: the state is read after the write, so it
    # is the state that refused it.
    state = await query_exercise_session_state(db, session_id)
    if state is None:
        raise HTTPException(status_code=404)
    if state.closed_at is not None:
        why = "The session is closed, so it is not paced any more."
    elif state.mode is ExerciseSessionMode.open:
        why = "The session is open: every exercise is already unlocked."
    elif from_ < 0:
        why = "The request did not say where the session stood."
    else:
        why = (
            "The session changed since this page was loaded - somebody else may "
            "have moved it. This is where it stands now."
        )
    return await _render_exercise_session_controls(
        request, db, course_id, session_id, why + " Nothing was changed.", view=view
    )


@router.post("/courses/{course_id}/sessions/{session_id}/open")
async def post_exercise_session_open(
    course_id: int, session_id: int, request: Request, db: DBDep, role: ExerciseSessionEditor
):
    """Switch a live session to open mode: everything unlocked, no pacing."""
    changed = await open_exercise_session_to_all(db, session_id)
    await db.commit()
    if not changed:
        return await _render_exercise_session_controls(
            request, db, course_id, session_id,
            "Only a live session that is not closed can be opened up. Nothing was changed.",
        )
    return await _render_exercise_session_controls(
        request, db, course_id, session_id, "The session is open: every exercise is unlocked.", ok=True
    )


@router.post("/courses/{course_id}/sessions/{session_id}/close")
async def post_exercise_session_close(
    course_id: int, session_id: int, request: Request, db: DBDep, role: ExerciseSessionEditor
):
    """Close a session for good. Students can still review their work in it;
    nothing new is accepted."""
    closed = await close_exercise_session(db, session_id)
    await db.commit()
    return await _render_exercise_session_controls(
        request,
        db,
        course_id,
        session_id,
        "The session is closed." if closed else "The session was already closed.",
        ok=closed,
    )


@router.post("/courses/{course_id}/sessions/{session_id}/reveal")
async def post_exercise_session_reveal(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    role: ExerciseSessionEditor,
    n: Annotated[int, Form()] = 0,
    view: Annotated[str, Form()] = "panel",
):
    """Show exercise `n`'s solution to the session's class, under its
    statement. There is no hiding it again: the class has seen it. Allowed on
    a closed session too, since reviewing is what a closed session is for."""
    revealed = await reveal_exercise_solution(db, session_id, n)
    await db.commit()
    if not revealed:
        raise HTTPException(status_code=404)
    return await _render_exercise_session_controls(
        request, db, course_id, session_id, view=view
    )


@router.post("/courses/{course_id}/sessions/{session_id}/switch")
async def post_exercise_session_switch(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    role: ExerciseSessionEditor,
    version_id: Annotated[int, Form()] = 0,
):
    """Move the session to the set's current version - the one the
    confirmation listed, named by `version_id`, so a version that became
    current after the page was drawn is refused rather than switched to
    unseen.

    Under the session's row lock (`FOR UPDATE`), which a job being queued
    holds `FOR SHARE` (`session_job_refusal`): the count of the session's
    checks that the refusal is decided on cannot be overtaken by a check
    landing on an exercise the new version does not have.
    """

    async def refuse(sentence: str):
        await db.rollback()
        return await _render_exercise_session_controls(
            request, db, course_id, session_id, sentence + " Nothing was changed."
        )

    state = await query_exercise_session_state(db, session_id, lock="update")
    if state is None:
        await db.rollback()
        raise HTTPException(status_code=404)
    if state.closed_at is not None:
        return await refuse("The session is closed, so its version is final.")
    current = await query_set_current_version(db, state.set_id)
    if current is None or current == state.version_id:
        return await refuse("The session is already on the latest version.")
    if current != version_id:
        return await refuse(
            "A newer version became current since this page was loaded. Look "
            "at what it changes and confirm again."
        )
    n_new = await query_version_exercise_count(db, current)
    max_checked = await query_max_checked_exercise(db, session_id)
    if max_checked is not None and max_checked > n_new:
        checked = await query_exercise(db, state.version_id, max_checked)
        session = await query_exercise_session(db, session_id)
        return await refuse(
            _switch_refusal(
                session["latest"]["number"],
                n_new,
                max_checked,
                checked.title if checked is not None else f"exercise {max_checked}",
            )
        )
    await switch_exercise_session_version(db, session_id, current, n_new)
    await db.commit()
    return await _render_exercise_session_controls(
        request, db, course_id, session_id, "Switched to the latest version.", ok=True
    )


def _exercise_status(check: dict | None) -> dict:
    """One person's status on one exercise, out of their aggregate row of
    `_session_checks` (None for no check): the strip's and the
    dashboard's, spelled once so the two cannot disagree."""
    if check is None:
        status = "not_attempted"
    else:
        status = "solved" if check["solved"] else "attempted"
    return {"status": status, "checking": check is not None and check["checking"]}


########################################################################
# The teacher's two screens of a session: the dashboard, with names, and the
# projector, without. Course staff only, through `ExerciseSessionEditor`, and
# both read `query_session_progress` - one document, polled every 3 s. They
# have a segment of their own after the session, which `get_session_exercise`
# would swallow were it not declared with `{n:int}`.


def session_progress_view(progress: dict) -> dict:
    """The dashboard's document out of `query_session_progress`: per
    exercise, how many of the cohort are in each status; per person, their
    status on every exercise of the version the session is on.

    Every exercise is there, the locked ones included - this is staff's - and
    `n_visible` says how far the students can see, so the grid can dim the
    rest. A check on an exercise the version no longer has (none can exist:
    the switch refuses that) would simply not be shown.
    """
    titles = progress["titles"]
    people = []
    for person in progress["participants"]:
        checks = {check["n"]: check for check in person["checks"]}
        people.append(
            {
                "id": person["id"],
                "nickname": person["nickname"],
                "email": person["email"],
                "cells": [
                    {"n": n, **_exercise_status(checks.get(n))}
                    for n in range(1, len(titles) + 1)
                ],
            }
        )
    exercises = []
    for n, title in enumerate(titles, start=1):
        cells = [person["cells"][n - 1] for person in people]
        counts = Counter(cell["status"] for cell in cells)
        exercises.append(
            {
                "n": n,
                "title": title,
                "solved": counts["solved"],
                "attempted": counts["attempted"],
                "not_attempted": counts["not_attempted"],
                "checking": sum(cell["checking"] for cell in cells),
            }
        )
    return {
        "label": progress["label"],
        "mode": progress["mode"],
        "closed_at": progress["closed_at"],
        "unlocked_through": progress["unlocked_through"],
        "n_visible": _n_visible(progress["mode"], progress["unlocked_through"], len(titles)),
        "revealed": progress["revealed"],
        "exercises": exercises,
        "participants": people,
        # Closing a session refuses new jobs and cancels none, so a check
        # queued before the close still lands after it: the grid polls until
        # there is none left.
        "pending": any(
            check["checking"]
            for person in progress["participants"]
            for check in person["checks"]
        ),
    }


def projector_view(counts: dict) -> dict:
    """The projector's document out of `query_session_counts`: per exercise
    the students can see, how many of the cohort solved it, tried it and are
    not there yet, and how many more are locked.

    The statuses are `_exercise_status`'s, worked out from counts rather than
    from one person's row: a person who checked an exercise has solved it or
    attempted it, and everybody else in the cohort has not attempted it. Kept
    in a function rather than in a template, `visible_outcomes`'s rule: the
    titles past the pacing stay off the projected screen, as they stay off
    the student's strip, and the query already brought no names to leave out.
    """
    titles = counts["titles"]
    total = counts["n_participants"]
    per_exercise = {row["n"]: row for row in counts["exercises"]}
    visible = _n_visible(counts["mode"], counts["unlocked_through"], len(titles))
    exercises = []
    for n, title in enumerate(titles[:visible], start=1):
        row = per_exercise.get(n, {"n_checked": 0, "n_solved": 0})
        exercises.append(
            {
                "n": n,
                "title": title,
                "solved": row["n_solved"],
                "attempted": row["n_checked"] - row["n_solved"],
                "not_attempted": total - row["n_checked"],
            }
        )
    return {
        "label": counts["label"],
        "mode": counts["mode"],
        "closed_at": counts["closed_at"],
        "unlocked_through": counts["unlocked_through"],
        "n_exercises": len(titles),
        "n_participants": total,
        "revealed": counts["revealed"],
        "exercises": exercises,
        "n_locked": len(titles) - visible,
        # `session_progress_view`'s reason: checks queued before a close
        # still land after it, and the bars should show them.
        "pending": any(row["checking"] for row in counts["exercises"]),
    }


async def _session_progress(db: AsyncConnection, session_id: int) -> dict:
    progress = await query_session_progress(db, session_id)
    if progress is None:
        raise HTTPException(status_code=404)
    return progress


@router.get("/courses/{course_id}/sessions/{session_id}/dashboard")
async def get_exercise_dashboard(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionEditor,
):
    """The class at a glance, with names: a grid of the cohort by the
    exercises, each cell a person's status, and - beside it, outside the poll
    - the code and results of the cell last clicked."""
    course = await query_course(db, course_id)
    session = await query_exercise_session(db, session_id)
    if course is None or session is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="exercise-dashboard.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "session": session,
            "user": user,
            "progress": session_progress_view(await _session_progress(db, session_id)),
        },
    )


@router.get("/f/courses/{course_id}/sessions/{session_id}/dashboard")
async def get_fragment_exercise_dashboard(
    course_id: int, session_id: int, request: Request, db: DBDep, role: ExerciseSessionEditor
):
    """The dashboard's grid, polled every three seconds."""
    return templates.TemplateResponse(
        request=request,
        name="f-exercise-dashboard.jinja",
        context={
            "course_id": course_id,
            "session_id": session_id,
            "progress": session_progress_view(await _session_progress(db, session_id)),
        },
    )


@router.get("/f/courses/{course_id}/sessions/{session_id}/dashboard/{user_id}/{n:int}")
async def get_fragment_exercise_drill_in(
    course_id: int,
    session_id: int,
    user_id: int,
    n: int,
    request: Request,
    db: DBDep,
    role: ExerciseSessionEditor,
):
    """One cell of the dashboard opened up: that person's latest Check and
    latest Run of exercise `n`, each with the code it sent and its result in
    full - staff are not redacted for - and each read against the version it
    *ran on*, which after a switch need not be the session's.

    Only somebody of the session's cohort (`query_session_cohort_member`), so
    the URL cannot be used to look anybody else up. It polls itself while a
    job of it is pending, and stops once none is.
    """
    state = await query_exercise_session_state(db, session_id)
    if state is None:
        raise HTTPException(status_code=404)
    _session_exercise_in_range(state, n)
    person = await query_session_cohort_member(db, session_id, user_id)
    if person is None:
        raise HTTPException(status_code=404)
    exercise = await query_exercise(db, state.version_id, n)
    last = await query_last_session_jobs(db, session_id, user_id, n)
    jobs = {}
    for kind in ("check", "run"):
        job = last.get(kind)
        jobs[kind] = await _exercise_job_context(
            db,
            job,
            None,
            redact=False,
            version_id=state.version_id,
            exercise=exercise if kind == "check" else None,
        )
    pending = any(
        job is not None and job.status is not ExerciseJobStatus.done for job in last.values()
    )
    return templates.TemplateResponse(
        request=request,
        name="f-exercise-drill-in.jinja",
        context={
            "course_id": course_id,
            "session_id": session_id,
            "person": person,
            "n": n,
            "title": exercise.title if exercise is not None else f"Exercise {n}",
            "last": last,
            "jobs": jobs,
            "poll_url": str(request.url) if pending else None,
        },
    )


async def _render_exercise_projector(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    session_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The projector's fragment: the bars, the pacing and the reveal buttons,
    one swap target that polls itself and that the pacing and reveal routes
    answer with when the press came from the projector."""
    return templates.TemplateResponse(
        request=request,
        name="f-exercise-projector.jinja",
        context=await _projector_context(db, course_id, session_id, message, ok),
    )


async def _projector_context(
    db: AsyncConnection,
    course_id: int,
    session_id: int,
    message: str | None = None,
    ok: bool = False,
) -> dict:
    counts = await query_session_counts(db, session_id)
    if counts is None:
        raise HTTPException(status_code=404)
    return {
        "course_id": course_id,
        "session_id": session_id,
        "projector": projector_view(counts),
        "message": message,
        "ok": ok,
    }


@router.get("/courses/{course_id}/sessions/{session_id}/projector")
async def get_exercise_projector(
    course_id: int,
    session_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ExerciseSessionEditor,
    solution: int = 0,
):
    """The screen the class sees: no names, one bar per exercise the class
    can see, and the controls the teacher drives the lesson with.

    `?solution=n` puts a revealed exercise's solution under the bars, outside
    the polled fragment so the poll does not close it. An exercise whose
    solution is not revealed shows none, whatever the URL says: a projected
    solution is a revealed one.
    """
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    context = await _projector_context(db, course_id, session_id)
    projector = context["projector"]
    shown = None
    if solution in projector["revealed"]:
        state = await query_exercise_session_state(db, session_id)
        if state is None:
            raise HTTPException(status_code=404)
        exercise = await query_exercise(db, state.version_id, solution)
        if exercise is not None:
            shown = {"n": solution, "title": exercise.title, "source": exercise.solution}
    context.update(course=course, user=user, solution=shown)
    return templates.TemplateResponse(
        request=request, name="exercise-projector.jinja", context=context
    )


@router.get("/f/courses/{course_id}/sessions/{session_id}/projector")
async def get_fragment_exercise_projector(
    course_id: int, session_id: int, request: Request, db: DBDep, role: ExerciseSessionEditor
):
    """The projector's fragment, polled every three seconds."""
    return await _render_exercise_projector(request, db, course_id, session_id)
