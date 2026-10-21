from dataclasses import dataclass
from typing import Annotated
from sqlalchemy import select
from cocco.avatar import MAX_AVATAR_BYTES, generate_identicon, sniff_image_mime
from cocco.evaluators import DEFAULT_EVALUATOR, bundled_path
from cocco.storage import safe_filename
from cocco.model import (
    ExerciseVersionStatus,
    LeaderboardRanking,
    User,
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
from cocco.repository.challenges import (
    count_testcase_outcomes,
    delete_testcase,
    insert_testcase,
    query_challenge_config,
    query_challenge_testcases,
    query_editable_challenges,
    query_evaluator_path,
    query_testcase,
    swap_testcase_order,
    update_challenge,
    update_testcase,
)
from cocco.repository.exercise_sets import (
    lock_exercise_set_for_push,
    query_editable_exercise_sets,
    query_exercise_set_course,
    query_exercise_version_summary,
)

from cocco.web import (
    admin,
    challenge_editor,
    challenges,
    core,
    courses,
    exercise_sessions,
    exercise_sets,
    exercise_student,
    home,
    people,
    signin,
)
from cocco.web.access import (
    ApiChallengeEditor,
    ApiUser,
    Blocked,
    ChallengeEditor,
    CurrentUser,
    Forbidden,
    NotAuthenticated,
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
from cocco.web.exercise_sets import (
    _import_exercise_file,
    _read_exercise_file,
)
from cocco.web.signin import _configured_oauth_providers, _require_password_login
from cocco.web.challenge_editor import _challenge_fields
from cocco.web.testcases import (
    _evaluator_context,
    _evaluator_dir,
    _render_testcases,
    _testcase_dir,
)
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
app.include_router(exercise_sessions.router)
app.include_router(exercise_student.router)
app.include_router(challenges.router)
app.include_router(challenge_editor.router)


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
