"""Test cases and the evaluator: where their files live, and the routes that change them."""

import io
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
)
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from cocco.evaluators import BUNDLED, DEFAULT_EVALUATOR, bundled_path
from cocco.repository.challenges import (
    count_testcase_outcomes,
    delete_testcase,
    insert_testcase,
    query_challenge_testcases,
    query_evaluator_path,
    query_testcase,
    swap_testcase_order,
    update_challenge,
    update_testcase,
)
from cocco.storage import safe_filename
from cocco.web import (
    core,
)
from cocco.web.access import (
    ChallengeEditor,
    CurrentUser,
)
from cocco.web.core import (
    EXTENSION_TO_LANGUAGE,
    MAX_UPLOAD_BYTES,
    DBDep,
    _stored_file_download,
)
from cocco.web.templating import (
    templates,
)

router = APIRouter()


# An evaluator is a source file, not test data, so it lives under the same
# ceiling as a submitted solution rather than the test case one.
MAX_EVALUATOR_BYTES = MAX_UPLOAD_BYTES

# Test case inputs are routinely far larger than a solution file, so they get
# their own, much higher ceiling - and are streamed to disk, never buffered.
MAX_TESTCASE_BYTES = 32 << 20


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
    core.storage.save_stream(new_path, io.BytesIO(contents))
    await update_challenge(db, challenge_id, evaluator_path=new_path)
    await db.commit()
    if previous is not None and previous != new_path:
        core.storage.delete(previous)


@router.post("/courses/{course_id}/{challenge_id}/evaluator")
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


@router.post("/courses/{course_id}/{challenge_id}/evaluator/bundled")
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


@router.post("/courses/{course_id}/{challenge_id}/evaluator/reset")
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


@router.get("/courses/{course_id}/{challenge_id}/evaluator/download")
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


@router.post("/f/markdown-preview")
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


@router.post("/courses/{course_id}/{challenge_id}/testcases")
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


@router.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/delete")
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


@router.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/rename")
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


@router.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/sample")
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


@router.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/active")
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


@router.post("/courses/{course_id}/{challenge_id}/testcases/{testcase_id}/move")
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

