"""Reference solutions: the editor card that adds, labels, re-runs and deletes them.

A reference is a flagged submission (`submission.reference_label`), so the judge
grades it like any other. Every route here is behind `ChallengeEditor` and
answers the whole card (`#references`, `f-challenge-references.jinja`), alert
included, so the message and the new state arrive in one swap.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import IntegrityError

from cocco.model import ReferenceVisibility
from cocco.repository.challenges import (
    insert_reference,
    query_challenge_references,
    query_challenge_score_threshold,
    query_reference,
    query_reference_by_filename,
    requeue_reference,
    update_reference,
)
from cocco.repository.lifecycle import delete_reference
from cocco.storage import safe_filename
from cocco.web.access import ChallengeEditor, CurrentUser
from cocco.web.challenges import read_solution_upload
from cocco.web.core import DBDep
from cocco.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


async def _references_context(db: DBDep, course_id: int, challenge_id: int) -> dict:
    """What `f-challenge-references.jinja` needs, whether it is served on its
    own or included by the editor page."""
    return {
        "course_id": course_id,
        "challenge_id": challenge_id,
        "references": await query_challenge_references(db, challenge_id),
        "score_threshold": await query_challenge_score_threshold(db, challenge_id),
    }


async def _render_references(
    request: Request,
    db: DBDep,
    course_id: int,
    challenge_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The card, from the database's own answer."""
    context = await _references_context(db, course_id, challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-challenge-references.jinja",
        context={**context, "message": message, "ok": ok},
    )


async def _the_reference(db: DBDep, challenge_id: int, submission_id: int):
    """The reference, or a 404: a student's submission, another challenge's
    reference and a missing id are all the same answer."""
    reference = await query_reference(db, challenge_id, submission_id)
    if reference is None:
        raise HTTPException(status_code=404)
    return reference


@router.get("/f/references/{course_id}/{challenge_id}")
async def get_fragment_references(
    course_id: int, challenge_id: int, request: Request, db: DBDep,
    role: ChallengeEditor,
):
    """The card's own poll while something is queued or running."""
    return await _render_references(request, db, course_id, challenge_id)


@router.post("/courses/{course_id}/{challenge_id}/references")
async def post_reference(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeEditor,
    solution: Annotated[UploadFile, File()],
    label: Annotated[str, Form()] = "",
):
    """Add a reference solution: an upload exactly like a student's, queued and
    hidden. A blank label becomes the file name, so there is no "label required"
    refusal. No archive check: adding one is editing, which archiving allows."""
    parsed = await read_solution_upload(solution)
    if isinstance(parsed, str):
        return await _render_references(request, db, course_id, challenge_id, parsed)
    filename, language, source = parsed

    # The file name is the reference's identity in `cocco pull`, so it must be
    # a bare safe name, and unique among this challenge's references.
    try:
        unsafe = safe_filename(filename) != filename
    except ValueError:
        unsafe = True
    if unsafe:
        return await _render_references(
            request, db, course_id, challenge_id,
            "Name the file with letters, digits, dot, dash and underscore only "
            "(no leading dot). It becomes the file's name in `cocco pull`.",
        )
    taken = (
        f"A reference named {filename} already exists: delete it first, or "
        "rename your file."
    )
    if await query_reference_by_filename(db, challenge_id, filename) is not None:
        return await _render_references(request, db, course_id, challenge_id, taken)

    try:
        await insert_reference(
            db,
            user_id=user.id,
            challenge_id=challenge_id,
            language=language,
            filename=filename,
            source=source,
            label=label.strip() or filename,
        )
        await db.commit()
    except IntegrityError:
        # Lost the race with another upload of the same name: the unique index
        # is the backstop for the check above.
        await db.rollback()
        return await _render_references(request, db, course_id, challenge_id, taken)
    logger.info("reference added", extra={"challenge_id": challenge_id})
    return await _render_references(
        request, db, course_id, challenge_id,
        f"{filename} was queued as a {language} reference solution. It is "
        "staff only until you change its visibility.",
        ok=True,
    )


@router.post("/courses/{course_id}/{challenge_id}/references/{submission_id}")
async def post_reference_update(
    course_id: int,
    challenge_id: int,
    submission_id: int,
    request: Request,
    db: DBDep,
    role: ChallengeEditor,
    label: Annotated[str | None, Form()] = None,
    visibility: Annotated[ReferenceVisibility | None, Form()] = None,
):
    """Rename and/or change what students see; a field that is absent is left
    alone. A blank label is refused with an alert rather than ignored: the box
    snaps back to the stored label, and saying why beats a silent revert."""
    await _the_reference(db, challenge_id, submission_id)
    if label is not None and not label.strip():
        return await _render_references(
            request, db, course_id, challenge_id, "A reference needs a label."
        )
    await update_reference(
        db,
        submission_id,
        label=None if label is None else label.strip(),
        visibility=visibility,
    )
    await db.commit()
    return await _render_references(
        request, db, course_id, challenge_id, "Saved.", ok=True
    )


@router.post("/courses/{course_id}/{challenge_id}/references/{submission_id}/rerun")
async def post_reference_rerun(
    course_id: int, challenge_id: int, submission_id: int, request: Request,
    db: DBDep, role: ChallengeEditor,
):
    await _the_reference(db, challenge_id, submission_id)
    if not await requeue_reference(db, submission_id):
        return await _render_references(
            request, db, course_id, challenge_id,
            "A judge is running this reference right now. Try again in a moment.",
        )
    await db.commit()
    return await _render_references(
        request, db, course_id, challenge_id, "Queued to run again.", ok=True
    )


@router.post("/courses/{course_id}/{challenge_id}/references/{submission_id}/delete")
async def post_reference_delete(
    course_id: int, challenge_id: int, submission_id: int, request: Request,
    db: DBDep, role: ChallengeEditor,
):
    await _the_reference(db, challenge_id, submission_id)
    if not await delete_reference(db, submission_id):
        return await _render_references(
            request, db, course_id, challenge_id,
            "A judge is running this reference right now. Nothing was deleted — "
            "try again in a moment.",
        )
    await db.commit()
    return await _render_references(
        request, db, course_id, challenge_id, "Reference deleted.", ok=True
    )
