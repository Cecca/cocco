"""The challenge editor: creating a challenge, editing it, and the danger zone."""

import math
from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import (
    Response,
)
from sqlalchemy import func

from cocco.model import (
    LeaderboardRanking,
)
from cocco.repository.challenges import (
    insert_challenge,
    query_challenge,
    query_challenge_config,
    query_challenge_testcases,
    update_challenge,
)
from cocco.repository.courses import (
    query_course,
)
from cocco.repository.lifecycle import (
    delete_challenge,
    lock_challenge_submissions,
    query_challenge_file_paths,
    query_challenge_visibility,
    set_challenge_archived,
)
from cocco.web import (
    core,
)
from cocco.web.access import (
    ChallengeEditor,
    CourseEditor,
    CurrentUser,
)
from cocco.web.core import (
    DBDep,
)
from cocco.web.courses import _confirmation_matches, _danger_alert, _judge_busy_message
from cocco.web.templating import (
    templates,
)
from cocco.web.references import _references_context
from cocco.web.testcases import _evaluator_context, _evaluator_dir, _testcase_dir

router = APIRouter()


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
    # No threshold: every graded submission counts. The form warns about it.
    "score_threshold": None,
    # Hidden, which is what `insert_challenge` writes too. A challenge is
    # published deliberately, from the editor's visibility strip, rather than
    # by default.
    "visible_from": None,
}


def _limit_fields(
    max_time_s: float, max_memory_mb: float, max_threads: int
) -> tuple[dict | None, str | None]:
    """The sandbox limits as columns, or the message explaining why not."""
    if max_time_s <= 0 or max_memory_mb <= 0 or max_threads < 1:
        return None, "Limits must be positive."
    return {
        "max_time_s": max_time_s,
        "max_memory_bytes": int(max_memory_mb * 1024 * 1024),
        "max_threads": max_threads,
    }, None


def _leaderboard_fields(
    ranking: str, score_threshold: str | float | None
) -> tuple[dict | None, str | None]:
    """The leaderboard's criterion and pass threshold as columns, or the message
    explaining why not."""
    try:
        criterion = LeaderboardRanking(ranking)
    except ValueError:
        return None, "Unknown leaderboard ranking."

    # The form posts text (empty = no threshold); a pushed `challenge.toml`
    # hands over a number or None. Either way it has to be finite: `nan` and
    # `inf` parse as floats and would make every comparison quietly false.
    threshold: float | None = None
    if isinstance(score_threshold, str):
        score_threshold = score_threshold.strip() or None
    if score_threshold is not None:
        try:
            threshold = float(score_threshold)
        except ValueError:
            return None, "The score threshold must be a number."
        if not math.isfinite(threshold):
            return None, "The score threshold must be a number."

    return {"ranking": criterion, "score_threshold": threshold}, None


def _visible_from_value(
    visible_from: str | datetime | None,
) -> tuple[dict | None, str | None]:
    """`visible_from` as a column, or the message explaining why not.

    It arrives in whichever shape its caller naturally holds: the
    `"2026-09-01T09:00"` an `<input type="datetime-local">` posts, the real
    `datetime` `tomllib` parses out of a pushed `challenge.toml`, or None for a
    challenge that is hidden. Normalizing all three here is what stops the
    browser and `cocco push` disagreeing about what a date means - and a naive
    value is read as the app server's local clock, which is the one clock
    `render_datetime` prints in and `render_datetime_local` fills the box with.
    """
    if isinstance(visible_from, str):
        try:
            visible_from = datetime.fromisoformat(visible_from)
        except ValueError:
            return None, "That is not a date and time."
    return {"visible_from": _as_aware(visible_from)}, None


def _challenge_fields(
    description: str,
    max_time_s: float,
    max_memory_mb: float,
    max_threads: int,
    ranking: str,
    visible_from: str | datetime | None = None,
    score_threshold: str | float | None = None,
) -> tuple[dict | None, str | None]:
    """The columns a whole challenge form writes, or the message explaining why
    not.

    The composition of the per-card validators, for the callers that save every
    column at once: `post_challenge_new` and `api_import_challenge`.
    """
    fields: dict = {"description": description}
    for part, error in (
        _limit_fields(max_time_s, max_memory_mb, max_threads),
        _leaderboard_fields(ranking, score_threshold),
        _visible_from_value(visible_from),
    ):
        if part is None:
            return None, error
        fields.update(part)
    return fields, None


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


@router.get("/courses/{course_id}/challenges/new")
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


@router.post("/courses/{course_id}/challenges/new")
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
    score_threshold: Annotated[str, Form()] = "",
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
        description,
        max_time_s,
        max_memory_mb,
        max_threads,
        ranking,
        None,
        score_threshold,
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


@router.get("/courses/{course_id}/{challenge_id}/edit")
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
            **await _references_context(db, course_id, challenge_id),
            "user": user,
            "challenge": challenge,
            "testcases": testcases,
            "title": f"Editing {challenge['title']}",
        },
    )


def _alert(request: Request, message: str, ok: bool = False):
    return templates.TemplateResponse(
        request=request,
        name="f-edit-result.jinja",
        context={"ok": ok, "message": message},
    )


def _render_card(
    request: Request,
    name: str,
    course_id: int,
    challenge,
    message: str | None = None,
    ok: bool = False,
):
    """One editor card as its own response: `challenge` is the stored row, or on
    a refused save the stored row with the posted values laid over it, so the
    teacher keeps what they typed."""
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "course_id": course_id,
            "challenge": challenge,
            "message": message,
            "ok": ok,
        },
    )


async def _visibility_card(
    request: Request,
    db,
    course_id: int,
    challenge_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The visibility strip, always from the database's own answer."""
    visible_from, is_visible = await query_challenge_visibility(db, challenge_id)
    challenge = {
        "id": challenge_id,
        "visible_from": visible_from,
        "is_visible": is_visible,
    }
    return _render_card(
        request, "f-challenge-visibility.jinja", course_id, challenge, message, ok
    )


@router.post("/courses/{course_id}/{challenge_id}/edit/statement")
async def post_challenge_statement(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    description: Annotated[str, Form()],
):
    """Save the problem statement, and answer with only a status line: this is
    the one card where someone may still be typing while the request is in
    flight, and swapping the textarea would drop those keystrokes."""
    await update_challenge(db, challenge_id, description=description)
    await db.commit()
    return _alert(request, "Saved.", ok=True)


@router.post("/courses/{course_id}/{challenge_id}/edit/leaderboard")
async def post_challenge_leaderboard(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    ranking: Annotated[str, Form()],
    score_threshold: Annotated[str, Form()] = "",
):
    """Save the leaderboard's criterion and pass threshold, and answer with the
    whole card, so the "no threshold" warning comes and goes on save."""
    fields, error = _leaderboard_fields(ranking, score_threshold)
    if fields is None:
        stored = await query_challenge_config(db, challenge_id)
        if stored is None:
            raise HTTPException(status_code=404)
        posted = {**stored._mapping, "ranking": ranking}
        posted["score_threshold"] = score_threshold.strip() or None
        return _render_card(
            request,
            "f-challenge-leaderboard.jinja",
            course_id,
            posted,
            error or "Invalid leaderboard.",
        )
    await update_challenge(db, challenge_id, **fields)
    await db.commit()
    challenge = await query_challenge_config(db, challenge_id)
    return _render_card(
        request, "f-challenge-leaderboard.jinja", course_id, challenge, "Saved.", True
    )


@router.post("/courses/{course_id}/{challenge_id}/edit/limits")
async def post_challenge_limits(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    max_time_s: Annotated[float, Form()],
    max_memory_mb: Annotated[float, Form()],
    max_threads: Annotated[int, Form()],
):
    """Save the sandbox limits, and answer with the whole card."""
    fields, error = _limit_fields(max_time_s, max_memory_mb, max_threads)
    if fields is None:
        stored = await query_challenge_config(db, challenge_id)
        if stored is None:
            raise HTTPException(status_code=404)
        posted = {
            **stored._mapping,
            "max_time_s": max_time_s,
            "max_memory_bytes": max_memory_mb * 1024 * 1024,
            "max_threads": max_threads,
        }
        return _render_card(
            request,
            "f-challenge-limits.jinja",
            course_id,
            posted,
            error or "Invalid limits.",
        )
    await update_challenge(db, challenge_id, **fields)
    await db.commit()
    challenge = await query_challenge_config(db, challenge_id)
    return _render_card(
        request, "f-challenge-limits.jinja", course_id, challenge, "Saved.", True
    )


@router.post("/courses/{course_id}/{challenge_id}/publish")
async def post_challenge_publish(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """Open the challenge to the class now. A hidden or scheduled one gets
    Postgres's `now()`, like every other timestamp; one that is already live
    keeps the date it went live (a stale tab can still post this, and an edit is
    not a republication)."""
    _, is_visible = await query_challenge_visibility(db, challenge_id)
    if not is_visible:
        await update_challenge(db, challenge_id, visible_from=func.now())
        await db.commit()
    return await _visibility_card(
        request, db, course_id, challenge_id, "Published.", True
    )


@router.post("/courses/{course_id}/{challenge_id}/schedule")
async def post_challenge_schedule(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    visible_at: Annotated[str, Form()] = "",
):
    """Open the challenge at a chosen moment. Past ones are accepted, as with
    `cocco push`: they simply mean visible now."""
    if not visible_at.strip():
        return await _visibility_card(
            request,
            db,
            course_id,
            challenge_id,
            "Pick the date and time the challenge opens.",
        )
    fields, error = _visible_from_value(visible_at.strip())
    if fields is None:
        return await _visibility_card(
            request, db, course_id, challenge_id, error or "Invalid date."
        )
    await update_challenge(db, challenge_id, **fields)
    await db.commit()
    return await _visibility_card(
        request, db, course_id, challenge_id, "Scheduled.", True
    )


@router.post("/courses/{course_id}/{challenge_id}/hide")
async def post_challenge_hide(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
):
    """Take the challenge off the class's pages."""
    await update_challenge(db, challenge_id, visible_from=None)
    await db.commit()
    return await _visibility_card(request, db, course_id, challenge_id, "Hidden.", True)


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


@router.post("/courses/{course_id}/{challenge_id}/archive")
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


@router.post("/courses/{course_id}/{challenge_id}/unarchive")
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


@router.post("/courses/{course_id}/{challenge_id}/delete")
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
