from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from sqlalchemy import select
from cocco.avatar import MAX_AVATAR_BYTES, generate_identicon, sniff_image_mime
from cocco.evaluators import BUNDLED, DEFAULT_EVALUATOR, bundled_path
from cocco.exercises.compare import describe_rules
from cocco.storage import safe_filename
from cocco.model import (
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSessionMode,
    ExerciseVersionStatus,
    LeaderboardRanking,
    User,
    UserRole,
)
from cocco import schema
from cocco.logs import AccessLogMiddleware
from cocco.tokens import hash_token, mint_token
from cocco.passwords import (
    MIN_PASSWORD_LENGTH,
    check_password,
    hash_password,
    verify_password,
)
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
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
import io
import logging
import os
import tempfile
import zipfile
from pathlib import Path

from cocco.repository.users import (
    is_administrator,
    query_user,
    query_user_profile,
    update_user,
)
from cocco.repository.credentials import (
    delete_api_token,
    delete_user_password,
    insert_api_token,
    query_api_tokens,
    query_password_hash,
    query_password_state,
    set_user_password,
)
from cocco.repository.courses import (
    query_course,
    query_user_role,
)
from cocco.repository.challenges import (
    count_running_submissions,
    count_testcase_outcomes,
    delete_testcase,
    insert_challenge,
    insert_submission,
    insert_testcase,
    query_challenge,
    query_challenge_config,
    query_challenge_leaderboard,
    query_challenge_ranking,
    query_challenge_standings,
    query_challenge_testcases,
    query_editable_challenges,
    query_evaluator_path,
    query_sample_testcase,
    query_submission,
    query_submission_outcomes,
    query_testcase,
    query_user_submissions,
    regrade_challenge,
    swap_testcase_order,
    update_challenge,
    update_testcase,
)
from cocco.repository.exercise_sets import (
    insert_exercise_job,
    lock_exercise_set_for_push,
    query_editable_exercise_sets,
    query_exercise,
    query_exercise_set_course,
    query_exercise_version_summary,
)
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
    query_session_heading,
    query_session_cohort_member,
    query_session_counts,
    query_session_job,
    query_session_progress,
    query_session_strip,
    query_set_current_version,
    query_version_exercise_count,
    reveal_exercise_solution,
    switch_exercise_session_version,
)
from cocco.repository.lifecycle import (
    delete_challenge,
    delete_exercise_session,
    lock_challenge_submissions,
    query_challenge_file_paths,
    query_challenge_visibility,
    query_course_archived_at,
    query_effective_archived_at,
    set_challenge_archived,
)

from cocco.web import admin, core, courses, exercise_sets, home, people, signin
from cocco.web.access import (
    ApiChallengeEditor,
    ApiUser,
    Blocked,
    ChallengeEditor,
    ChallengeMember,
    ChallengeViewer,
    CourseEditor,
    CourseMember,
    CurrentUser,
    Forbidden,
    NotAuthenticated,
    authorize_submission,
    effective_course_role,
    handle_blocked,
    handle_forbidden,
    handle_not_authenticated,
)
from cocco.web.core import (
    DBDep,
    EDITOR_ROLES,
    EXTENSION_TO_LANGUAGE,
    MAX_UPLOAD_BYTES,
    _slug,
    _stored_file_download,
    _zip_stored,
)
from cocco.web.seed import (
    prepare_database,
)
from cocco.web.courses import _confirmation_matches, _danger_alert, _judge_busy_message
from cocco.web.exercise_sets import (
    ARCHIVED_EXERCISE_JOB_REFUSAL,
    ExerciseSetEditor,
    _exercise_job_context,
    _exercise_job_refusal,
    _import_exercise_file,
    _read_exercise_file,
    _render_exercise_job,
)
from cocco.web.signin import _configured_oauth_providers, _require_password_login
from cocco.web.testcases import _evaluator_dir, _testcase_dir
from cocco.web.templating import (
    STATIC_DIR,
    VersionedStaticFiles,
    templates,
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


@app.post("/courses/{course_id}/exercises/{set_id}/sessions")
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


@app.get("/courses/{course_id}/sessions/{session_id}")
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


@app.post("/courses/{course_id}/sessions/{session_id}/delete")
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


@app.post("/courses/{course_id}/sessions/{session_id}/pace")
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


@app.post("/courses/{course_id}/sessions/{session_id}/open")
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


@app.post("/courses/{course_id}/sessions/{session_id}/close")
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


@app.post("/courses/{course_id}/sessions/{session_id}/reveal")
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


@app.post("/courses/{course_id}/sessions/{session_id}/switch")
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


@app.get("/courses/{course_id}/sessions/{session_id}/dashboard")
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


@app.get("/f/courses/{course_id}/sessions/{session_id}/dashboard")
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


@app.get("/f/courses/{course_id}/sessions/{session_id}/dashboard/{user_id}/{n:int}")
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


@app.get("/courses/{course_id}/sessions/{session_id}/projector")
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


@app.get("/f/courses/{course_id}/sessions/{session_id}/projector")
async def get_fragment_exercise_projector(
    course_id: int, session_id: int, request: Request, db: DBDep, role: ExerciseSessionEditor
):
    """The projector's fragment, polled every three seconds."""
    return await _render_exercise_projector(request, db, course_id, session_id)


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


def _exercise_status(check: dict | None) -> dict:
    """One person's status on one exercise, out of their aggregate row of
    `_session_checks` (None for no check): the strip's and the
    dashboard's, spelled once so the two cannot disagree."""
    if check is None:
        status = "not_attempted"
    else:
        status = "solved" if check["solved"] else "attempted"
    return {"status": status, "checking": check is not None and check["checking"]}


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


@app.get("/courses/{course_id}/sessions/{session_id}/{n:int}")
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


@app.get("/f/courses/{course_id}/sessions/{session_id}/strip")
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


@app.post("/courses/{course_id}/sessions/{session_id}/{n:int}/run")
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


@app.post("/courses/{course_id}/sessions/{session_id}/{n:int}/check")
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


@app.get("/f/courses/{course_id}/sessions/{session_id}/jobs/{job_id}")
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
    if not core.storage.exists(relative):
        raise HTTPException(status_code=404)
    return _stored_file_download(
        relative, f"sample{testcase.execution_order + 1}_{kind}.txt"
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
    # Hidden, which is what `insert_challenge` writes too. The form's radio
    # group reads this, so the create page opens on "Hidden" and a challenge is
    # published deliberately rather than by default.
    "visible_from": None,
}


def _challenge_fields(
    description: str,
    max_time_s: float,
    max_memory_mb: float,
    max_threads: int,
    ranking: str,
    visible_from: str | datetime | None = None,
) -> tuple[dict | None, str | None]:
    """The columns a challenge form writes, or the message explaining why not.

    The create and the edit form are the same form - `_challenge-form.jinja` -
    so they are validated once, here, and the caller only decides what to do
    with the result. `api_import_challenge` is the third caller rather than a
    second opinion.

    `visible_from` arrives in whichever shape its caller naturally holds: the
    `"2026-09-01T09:00"` an `<input type="datetime-local">` posts, the real
    `datetime` `tomllib` parses out of a pushed `challenge.toml`, or None for a
    challenge that is hidden. Normalizing all three here is what stops the
    browser and `cocco push` disagreeing about what a date means - and a naive
    value is read as the app server's local clock, which is the one clock
    `render_datetime` prints in and `render_datetime_local` fills the box with.
    """
    if max_time_s <= 0 or max_memory_mb <= 0 or max_threads < 1:
        return None, "Limits must be positive."

    try:
        criterion = LeaderboardRanking(ranking)
    except ValueError:
        return None, "Unknown leaderboard ranking."

    if isinstance(visible_from, str):
        try:
            visible_from = datetime.fromisoformat(visible_from)
        except ValueError:
            return None, "That is not a date and time."
    visible_from = _as_aware(visible_from)

    return {
        "description": description,
        "max_time_s": max_time_s,
        "max_memory_bytes": int(max_memory_mb * 1024 * 1024),
        "max_threads": max_threads,
        "ranking": criterion,
        "visible_from": visible_from,
    }, None


def _as_aware(value: datetime | None) -> datetime | None:
    """A datetime with an offset on it, reading a naive one as this server's own
    clock.

    Naive values reach us from two directions and both mean the same thing: an
    `<input type="datetime-local">` posts a wall clock with no offset, and a
    `challenge.toml` may spell a TOML local-datetime. The column itself is
    `timestamptz` and Postgres hands its values back aware, so nothing read from
    the database arrives here needing this. Reading both on the server's clock
    is the same rule
    `render_datetime` prints in and `render_datetime_local` fills the box with,
    which is what stops a challenge opening an hour away from where it said.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.astimezone()


# What the Visibility card's radio group can say. `now` and `scheduled` both end
# up as a timestamp in the same column - the difference is only which one - and
# `hidden` is the NULL.
VISIBILITY_CHOICES = ("hidden", "now", "scheduled")


def _visible_from_choice(
    visibility: str, visible_at: str, current: datetime | None
) -> tuple[str | datetime | None, str | None]:
    """Turn the Visibility card's two fields into one value, or say why not.

    A radio and a box rather than a box alone, because "publish this now" is the
    commonest thing anybody does here and should not require typing a date; and
    one column behind them, because "hidden", "live" and "opens on Tuesday" are
    three answers to one question.

    `current` is what the challenge already says, and it exists for one case:
    re-saving a challenge that is already live must not re-stamp its publication
    date, or every edit to the statement would move the "visible since" a badge
    prints. A challenge whose date is still in the future is *not* preserved -
    picking `now` there is precisely the act of bringing it forward.
    """
    if visibility not in VISIBILITY_CHOICES:
        return None, "Unknown visibility."
    if visibility == "hidden":
        return None, None
    if visibility == "scheduled":
        if not visible_at.strip():
            return None, "Pick the date and time the challenge opens."
        return visible_at.strip(), None
    now = datetime.now().astimezone()
    current = _as_aware(current)
    if current is not None and current <= now:
        return current, None
    return now, None


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
    visibility: Annotated[str, Form()] = "hidden",
    visible_at: Annotated[str, Form()] = "",
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

    # No current value to preserve - there is no challenge yet - so `now` here
    # always means this moment.
    visible_from, error = _visible_from_choice(visibility, visible_at, None)
    if error is not None:
        return alert(error)

    fields, error = _challenge_fields(
        description, max_time_s, max_memory_mb, max_threads, ranking, visible_from
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
    visibility: Annotated[str, Form()] = "hidden",
    visible_at: Annotated[str, Form()] = "",
):
    """Save the problem statement, the sandbox limits, the leaderboard and when
    the class may see any of it."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    # The current value, so that saving a live challenge with "Visible now"
    # keeps the date it went live rather than re-stamping it on every edit.
    current, _ = await query_challenge_visibility(db, challenge_id)
    visible_from, error = _visible_from_choice(visibility, visible_at, current)
    if error is not None:
        return alert(error)

    fields, error = _challenge_fields(
        description, max_time_s, max_memory_mb, max_threads, ranking, visible_from
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
    this column - see `_effective_archived_at`."""
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
        core.storage.delete(path)
    core.storage.delete_directory(_testcase_dir(challenge_id))
    core.storage.delete_directory(_evaluator_dir(challenge_id))

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
    core.storage.save_stream(new_path, io.BytesIO(contents))
    await update_challenge(db, challenge_id, evaluator_path=new_path)
    await db.commit()
    if previous is not None and previous != new_path:
        core.storage.delete(previous)


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
        core.storage.delete(previous)
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
        # The bundled default lives in the source tree rather than in storage,
        # so it is plain and is served as the file it is.
        path = bundled_path(DEFAULT_EVALUATOR)
        if not path.is_file():
            raise HTTPException(status_code=404)
        # text/plain whatever the extension says, so a script named .html can
        # never be served as a page on this origin.
        return FileResponse(path, media_type="text/plain", filename=path.name)
    if not core.storage.exists(relative):
        raise HTTPException(status_code=404)
    return _stored_file_download(relative, Path(relative).name)


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
    if core.storage.exists(input_path) or core.storage.exists(output_path):
        return await _render_testcases(
            request, db, course_id, challenge_id,
            "A file with that name already exists for this challenge.", ok=False,
        )

    # Streamed, not read: test inputs can be several megabytes (DESIGN.md).
    # On a thread, because they are compressed on the way down and deflating
    # tens of megabytes on the event loop would stop every polled fragment in
    # the site for its duration - the rule `hash_password` is here for.
    await run_in_threadpool(core.storage.save_stream, input_path, input_file.file)
    await run_in_threadpool(
        core.storage.save_stream, output_path, expected_output_file.file
    )

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
    core.storage.delete(testcase.input_path)
    core.storage.delete(testcase.expected_output_path)
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
    if core.storage.exists(new_path):
        return await fail(f"{new_name} already exists for this challenge.")

    try:
        core.storage.rename(old_path, new_path)
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
# login identifier a provider proves, and `user.email` is unique on it.
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
            # The password section, on the same `is_owner` footing as the
            # tokens above and behind the same switch the login page is: with
            # `[auth] password_login` off the feature does not exist, and the
            # routes 404 rather than merely not being drawn.
            "password_login": core.CONFIG.auth.password_login,
            "password": (
                await query_password_state(db, user_id)
                if is_owner and core.CONFIG.auth.password_login
                else None
            ),
            "min_password_length": MIN_PASSWORD_LENGTH,
            # `f-password.jinja` says different things about removing a
            # password depending on whether there is another way in.
            "sso_configured": bool(_configured_oauth_providers()),
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


########################################################################
# The password, which lives beside the tokens because it is the same kind of
# thing: a credential of this account, on this account's own page. Every route
# here is behind `ProfileOwner` for the tokens' reason, one step further along
# - reading a profile is oversight, and setting somebody's password is not
# oversight but the ability to become them. An administrator who has to get an
# account back in uses `/admin`'s reset, which issues a password rather than
# choosing one, and which the owner is expected to replace from here.
#
# All of it is behind `[auth] password_login` as well, and a 404 rather than a
# hidden form, the rule `post_auth_dummy` states.


async def _render_password(
    request: Request,
    db: AsyncConnection,
    user_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The password section, as every mutation answers with it - one swap
    target with the alert folded in, which is `/admin`'s rule and
    `_render_tokens`'s shape."""
    return templates.TemplateResponse(
        request=request,
        name="f-password.jinja",
        context={
            "profile_id": user_id,
            "password": await query_password_state(db, user_id),
            "min_password_length": MIN_PASSWORD_LENGTH,
            "sso_configured": bool(_configured_oauth_providers()),
            "message": message,
            "ok": ok,
        },
    )


@app.get("/f/users/{user_id}/password")
async def get_fragment_password(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    _require_password_login()
    return await _render_password(request, db, user_id)


@app.post("/users/{user_id}/password")
async def post_password(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    # Defaulted, as everywhere else on this page: a missing field is a 422 and
    # htmx drops the body of an error response.
    current_password: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    """Set a password, or change the one that is there.

    The current password is required whenever there already is one, and is not
    a formality: a session left open on a shared machine is the ordinary way an
    account is taken, and without this check the person who found it could lock
    its owner out. There is nothing to ask for when no password is set yet -
    the session is the only thing that could have got here.
    """
    _require_password_login()

    existing = await query_password_state(db, user_id)
    if existing is not None:
        if not await run_in_threadpool(
            verify_password,
            current_password,
            await query_password_hash(db, owner.email),
        ):
            return await _render_password(
                request, db, user_id, "That is not your current password.", ok=False
            )

    try:
        check_password(password)
    except ValueError as err:
        return await _render_password(request, db, user_id, str(err), ok=False)

    if password != confirm:
        return await _render_password(
            request, db, user_id, "Those two passwords are not the same.", ok=False
        )

    await set_user_password(
        db, user_id, await run_in_threadpool(hash_password, password)
    )
    await db.commit()
    return await _render_password(
        request,
        db,
        user_id,
        "Password changed." if existing is not None else "Password set.",
        ok=True,
    )


@app.post("/users/{user_id}/password/delete")
async def post_password_delete(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    """Take the password off this account.

    Allowed even when it is the only way in, and deliberately: this is the
    owner's own account and the fragment says plainly what they are about to be
    left with. The app's usual line applies - nothing else goes with it, and
    `/admin` can issue a new one.
    """
    _require_password_login()

    removed = await delete_user_password(db, user_id)
    await db.commit()
    return await _render_password(
        request,
        db,
        user_id,
        "Password removed." if removed else "There was no password to remove.",
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
                # The same kind of note, for the other end of the life cycle.
                # `visible_from` is null for a hidden challenge, and `visible`
                # is false for a hidden *or* a scheduled one - the CLI prints
                # which, and neither is a gate: staff push to a challenge they
                # have not published precisely because they are still writing it.
                "visible_from": (
                    None if row.visible_from is None else row.visible_from.isoformat()
                ),
                "visible": bool(row.is_visible),
            }
            for row in rows
        ]
    )


@dataclass(frozen=True)
class _ChallengeSnapshot:
    """What an export of a challenge is made of, read once, so that a zip and
    the version it is sent with describe the same rows."""

    manifest: Manifest
    statement: str
    rows: list
    evaluator_path: str | None

    def stored_files(self) -> list[tuple[str, str]]:
        """`(path in storage, name in the archive)` for every file exported."""
        files = []
        for row, entry in zip(self.rows, self.manifest.testcases):
            files.append((row.input_path, entry.input))
            files.append((row.expected_output_path, entry.expected_output))
        if self.evaluator_path is not None and self.manifest.evaluator is not None:
            files.append((self.evaluator_path, self.manifest.evaluator))
        return files


async def _challenge_snapshot(
    db: AsyncConnection, challenge_id: int
) -> _ChallengeSnapshot | None:
    challenge = await query_challenge_config(db, challenge_id)
    if challenge is None:
        return None

    rows = list(await query_challenge_testcases(db, challenge_id))
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
        # Absent from the file when the challenge is hidden, which is what
        # `dump_manifest` does with a None - so pushing a pulled file back keeps
        # it hidden rather than publishing it.
        visible_from=challenge.visible_from,
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
    return _ChallengeSnapshot(
        manifest=manifest,
        statement=challenge.description or "",
        rows=rows,
        evaluator_path=evaluator_path,
    )


def _challenge_version(snapshot: _ChallengeSnapshot) -> str:
    """The version of a challenge a pull records and a push's `If-Match` names.

    Derived and never stored. A revision column would have to be bumped by
    every route that touches a challenge, its test cases or their files, and
    the one route that forgot would be a silent overwrite - the very thing the
    version is for. So it is a digest of what the export carries: the manifest,
    the statement, and each stored file's size and mtime rather than its bytes.
    Reading every test case to answer a precondition would cost what the export
    costs, and gzip's own header timestamp means the stored bytes of identical
    contents differ anyway. A file rewritten with the same contents therefore
    reads as a change, which costs one `cocco pull` that finds nothing to merge
    - the safe direction to be wrong in.
    """
    stamps = []
    for stored, name in snapshot.stored_files():
        size, mtime_ns = core.storage.stamp(stored) or (-1, -1)
        stamps.append((name, size, mtime_ns))
    return transfer.challenge_version(snapshot.manifest, snapshot.statement, stamps)


def _etag(version: str) -> str:
    return f'"{version}"'


def _etag_matches(header: str, version: str, *, weak: bool) -> bool:
    """Whether an `If-Match` or `If-None-Match` header names `version`.

    Either is `*` or a comma-separated list of entity tags. `If-None-Match`
    compares weakly, so a `W/` tag still matches; `If-Match` compares strongly
    and a weak tag never does (RFC 9110 §13.1).
    """
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            if not weak:
                continue
            candidate = candidate[2:]
        if candidate == _etag(version):
            return True
    return False


@app.get("/api/challenges/{challenge_id}")
async def api_export_challenge(
    challenge_id: int,
    db: DBDep,
    role: ApiChallengeEditor,
    if_none_match: Annotated[str | None, Header()] = None,
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

    The challenge's version travels as the `ETag` (see `_challenge_version`),
    and a request whose `If-None-Match` already names it is answered `304`
    before the zip is built - which is what makes `cocco pull` on a challenge
    nobody has touched cost one round trip rather than a download.
    """
    snapshot = await _challenge_snapshot(db, challenge_id)
    if snapshot is None:
        raise HTTPException(status_code=404)
    manifest, rows, evaluator_path = (
        snapshot.manifest, snapshot.rows, snapshot.evaluator_path
    )

    # A row may name a file that is not there - a failed commit after a write,
    # a storage root that moved, a database restored without its files. Asked
    # before the zip is opened, so the answer is a sentence naming the file
    # rather than a traceback halfway through a half-written download.
    for row in rows:
        for path in (row.input_path, row.expected_output_path):
            if not core.storage.exists(path):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This challenge cannot be exported: {path} is missing "
                        "from storage. The test case's row survived its file."
                    ),
                )
    if evaluator_path is not None and not core.storage.exists(evaluator_path):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This challenge cannot be exported: its evaluator "
                f"{evaluator_path} is missing from storage."
            ),
        )

    # Stamped before a single file is read, so a file rewritten while the zip
    # is being built makes the version older than the contents rather than
    # newer: the next push is refused needlessly, and never let through wrongly.
    version = _challenge_version(snapshot)
    if if_none_match is not None and _etag_matches(if_none_match, version, weak=True):
        return Response(status_code=304, headers={"ETag": _etag(version)})

    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)

    # Every member is now inflated out of storage and deflated back into the
    # zip, so the whole build goes on a thread rather than on the event loop -
    # `post_testcase`'s rule, read the other way round.
    def build() -> None:
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
            zf.writestr(STATEMENT_NAME, snapshot.statement)
            for stored, name in snapshot.stored_files():
                _zip_stored(zf, stored, name)
        handle.close()

    try:
        await run_in_threadpool(build)
    except (OSError, ValueError):
        handle.close()
        os.unlink(handle.name)
        raise

    return FileResponse(
        handle.name,
        media_type="application/zip",
        filename=f"challenge-{challenge_id}-{_slug(manifest.title)}.zip",
        # Given explicitly, because `FileResponse` would otherwise invent one
        # from the temporary file's mtime - a tag that changes on every export.
        headers={"ETag": _etag(version)},
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
    if_match: Annotated[str | None, Header()] = None,
):
    """Apply a pushed zip to the challenge named in the URL.

    A push based on an older version is a **412**, asked before the upload is
    read. `If-Match` names the version the pusher last pulled or pushed, so a
    mismatch means the challenge was changed since - on the web, or from
    another checkout - and applying the zip would silently undo that. Without
    the header a push is applied unconditionally, as it always was: that is
    `cocco push --force`, and a zip pushed by hand. The response carries the
    version the push produced, which is what lets the next one be conditional.
    The check and the write are not one transaction, so a web edit landing
    between them is still overwritten; closing that would take a lock every
    editor route agreed to take, for a window of one request.

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

    if if_match is not None:
        current = await _challenge_snapshot(db, challenge_id)
        if current is None:
            raise HTTPException(status_code=404)
        if not _etag_matches(if_match, _challenge_version(current), weak=False):
            return JSONResponse(
                {
                    "detail": (
                        "This challenge has changed since the version this push "
                        "was based on. Pull it again first."
                    )
                },
                status_code=412,
            )

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

        # The limits, the ranking and the visibility are validated by the same
        # function the create form and the edit form go through, rather than a
        # second time here: this is `_challenge_fields`' third caller, not its
        # rival. `visible_from` arrives as a `datetime` from `tomllib` where the
        # forms hand it a string, which is exactly the difference that function
        # exists to absorb.
        fields, error = _challenge_fields(
            statement,
            manifest.max_time_s,
            manifest.max_memory_mb,
            manifest.max_threads,
            manifest.ranking.value,
            manifest.visible_from,
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

            async def store(member: str, name: str) -> str:
                path = f"{directory}/{name}"

                # Inflate out of the zip and deflate into storage, both on a
                # thread: a push carries every test case of a challenge at
                # once, and `post_testcase`'s rule applies a hundredfold here.
                def save() -> None:
                    with archive.open(member) as source:
                        core.storage.save_stream(path, source)

                await run_in_threadpool(save)
                written.add(path)
                return path

            superseded: list[str] = []

            for update in plan.updated:
                await store(update.entry.input, update.entry.input_name)
                output_path = await store(
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
                input_path = await store(
                    addition.entry.input, addition.entry.input_name
                )
                output_path = await store(
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
                core.storage.save_stream(new_evaluator, io.BytesIO(evaluator_source))
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
                core.storage.delete(path)
    finally:
        upload.close()
        os.unlink(upload.name)

    # After the superseded files are gone, though they are not part of it: this
    # is the version the next export will report, so it must be read from the
    # same state that export will see.
    after = await _challenge_snapshot(db, challenge_id)
    version = None if after is None else _challenge_version(after)

    return JSONResponse(
        {
            "challenge_id": challenge_id,
            "version": version,
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


# ── Exercise sets ────────────────────────────────────────────────────────────
#
# A set is already a file - the source an import stores verbatim - so there is
# no zip and no manifest here: a pull is that text and a push is an import, the
# same `_import_exercise_file` the set page's upload goes through. What carries
# over from challenges is the discipline around it: JSON refusals with real
# status codes, the `ETag` a pull records and the `If-Match` a push sends.
#
# The version is the newest version's **id**, not a digest. A version is
# immutable - its source is written once, when it is created - so its id names
# its contents exactly, and every way a set's file can change, the web upload
# included, is a new row and a new id. There is nothing to derive.


async def require_api_exercise_set_editor(
    set_id: int, db: DBDep, user: ApiUser
) -> int:
    """`require_api_challenge_editor` for an exercise set: the course is looked
    up from the set, and the role resolved from the course. Returns the course,
    which an import needs.

    A set that does not exist is the same 403 as one on somebody else's
    course, as a challenge is, so the API does not say which ids are taken.
    """
    course_id = await query_exercise_set_course(db, set_id)
    role = (
        None if course_id is None
        else await effective_course_role(db, user, course_id)
    )
    if course_id is None or role is None or role not in EDITOR_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only this exercise set's course staff can do that.",
        )
    return course_id


ApiExerciseSetEditor = Annotated[int, Depends(require_api_exercise_set_editor)]


def _exercise_version_json(set_id: int, version) -> dict:
    """What a push answers and the build's progress is polled as."""
    return {
        "set_id": set_id,
        "version": str(version.id),
        "number": version.number,
        "title": version.title,
        "status": ExerciseVersionStatus(version.status).value,
        "current": bool(version.is_current),
        # NULL while the version builds; the build writes a list.
        "problems": version.problems or [],
    }


@app.get("/api/exercises")
async def api_list_exercise_sets(db: DBDep, user: ApiUser):
    """`api_list_challenges` for exercise sets."""
    rows = await query_editable_exercise_sets(
        db,
        user_id=user.id,
        editor_roles=EDITOR_ROLES,
        every_course=await is_administrator(db, user.email),
    )
    return JSONResponse(
        [
            {
                "course_id": row["course_id"],
                "course_title": row["course_title"],
                "set_id": row["set_id"],
                "title": row["title"],
                "archived": row["archived"],
                # The newest version, which is what a pull hands out, and the
                # current one, which is what sessions are opened on - two
                # numbers exactly while a re-import is building or has failed.
                "latest_number": row["latest_number"],
                "latest_status": row["latest_status"].value,
                "current_number": row["current_number"],
            }
            for row in rows
        ]
    )


@app.get("/api/exercises/{set_id}")
async def api_export_exercise_set(
    set_id: int,
    db: DBDep,
    course_id: ApiExerciseSetEditor,
    if_none_match: Annotated[str | None, Header()] = None,
):
    """The newest version's file, exactly as it was imported.

    The newest and not the current: see `query_exercise_version_summary`. Its
    status travels in `X-Cocco-Version-Status`, on a 304 as well, so a CLI can
    say that the file it holds is one that failed to build.
    """
    version = await query_exercise_version_summary(db, set_id, with_source=True)
    if version is None:
        raise HTTPException(status_code=404)
    headers = {
        "ETag": _etag(str(version.id)),
        "X-Cocco-Version-Number": str(version.number),
        "X-Cocco-Version-Status": ExerciseVersionStatus(version.status).value,
    }
    if if_none_match is not None and _etag_matches(
        if_none_match, str(version.id), weak=True
    ):
        return Response(status_code=304, headers=headers)
    filename = f"exercises-{set_id}-{_slug(version.title, 'exercises')}.md"
    return Response(
        content=version.source,
        media_type="text/markdown; charset=utf-8",
        headers={
            **headers,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/exercises/{set_id}")
async def api_import_exercise_set(
    set_id: int,
    db: DBDep,
    user: ApiUser,
    course_id: ApiExerciseSetEditor,
    file: Annotated[UploadFile, File()],
    if_match: Annotated[str | None, Header()] = None,
):
    """Import a pushed file as a new version of the set: the set page's upload
    by another route, so it parses here, is refused with its problems if it
    has errors, and otherwise becomes a `building` version with its build
    queued. Like that upload, it becomes current only once the build is ready.

    `If-Match` names the newest version the pusher has seen, and a push based
    on an older one is a 412 - a file re-imported on the web since would
    otherwise be silently replaced. Without the header the push is applied
    unconditionally, which is `cocco push --force`.

    The title defaults to the newest version's rather than to the uploaded
    file's name, which is always `exercises.md`: a file whose front matter
    names no title keeps the one the set already has.

    Deliberately an update and never a create, as a challenge push is: a new
    set is imported on its course page, and pushed to once it exists.
    """
    read = await _read_exercise_file(file)
    if isinstance(read, str):
        return JSONResponse({"detail": read}, status_code=400)
    source, _ = read

    # The check and the import are one transaction under the set's lock, so
    # two pushes based on the same version cannot both pass it - unlike a
    # challenge's push, where closing that window would take a lock every
    # editor route agreed to take. Here the import is the only write there is.
    if not await lock_exercise_set_for_push(db, set_id):
        raise HTTPException(status_code=404)
    latest = await query_exercise_version_summary(db, set_id)
    if latest is None:
        raise HTTPException(status_code=404)
    if if_match is not None and not _etag_matches(
        if_match, str(latest.id), weak=False
    ):
        await db.rollback()
        return JSONResponse(
            {
                "detail": (
                    "This exercise set has changed since the version this push "
                    "was based on. Pull it again first."
                )
            },
            status_code=412,
        )
    imported, problems = await _import_exercise_file(
        db, user, course_id, set_id, source, latest.title
    )
    if imported is None:
        return JSONResponse(
            {
                "detail": "The file has errors, so no version was created.",
                "problems": [p.model_dump(mode="json") for p in problems],
            },
            status_code=400,
        )

    _, version_id = imported
    version = await query_exercise_version_summary(db, set_id, version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return JSONResponse(
        {
            **_exercise_version_json(set_id, version),
            # The parse's warnings, now: the build finds them again and stores
            # them on the version, but a push that waits for nothing should
            # still hear about them.
            "problems": [p.model_dump(mode="json") for p in problems],
        }
    )


@app.get("/api/exercises/{set_id}/versions/{version_id}")
async def api_exercise_version(
    set_id: int, version_id: int, db: DBDep, course_id: ApiExerciseSetEditor
):
    """One version's status and problems, which `cocco push` polls until the
    build it queued has finished."""
    version = await query_exercise_version_summary(db, set_id, version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return JSONResponse(_exercise_version_json(set_id, version))
