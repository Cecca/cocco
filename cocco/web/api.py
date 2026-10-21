"""The transfer API: pull and push a challenge or an exercise set as a zip, for the cocco CLI."""

import io
import os
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from cocco import transfer
from cocco.model import (
    ExerciseVersionStatus,
    LeaderboardRanking,
)
from cocco.repository.challenges import (
    delete_testcase,
    insert_testcase,
    query_challenge_config,
    query_challenge_testcases,
    query_editable_challenges,
    query_evaluator_path,
    update_challenge,
    update_testcase,
)
from cocco.repository.exercise_sets import (
    lock_exercise_set_for_push,
    query_editable_exercise_sets,
    query_exercise_set_course,
    query_exercise_version_summary,
)
from cocco.repository.users import (
    is_administrator,
)
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
from cocco.web import (
    core,
)
from cocco.web.access import (
    ApiChallengeEditor,
    ApiUser,
    effective_course_role,
)
from cocco.web.challenge_editor import _challenge_fields
from cocco.web.core import (
    EDITOR_ROLES,
    DBDep,
    _slug,
    _zip_stored,
)
from cocco.web.exercise_sets import (
    _import_exercise_file,
    _read_exercise_file,
)
from cocco.web.testcases import (
    _check_evaluator_source,
    _evaluator_dir,
    _testcase_dir,
)

router = APIRouter()


# A pushed challenge carries every test case at once, so its ceiling is a
# multiple of one test case's rather than equal to it. Checked twice, on the
# compressed bytes as they arrive and on the *uncompressed* total declared in
# the zip's directory before a single member is read: a cap on compressed size
# alone is not a cap at all, which is what a zip bomb is.
MAX_TRANSFER_BYTES = 256 << 20


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


@router.get("/api/challenges")
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


@router.get("/api/challenges/{challenge_id}")
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


@router.post("/api/challenges/{challenge_id}")
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


@router.get("/api/exercises")
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


@router.get("/api/exercises/{set_id}")
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


@router.post("/api/exercises/{set_id}")
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


@router.get("/api/exercises/{set_id}/versions/{version_id}")
async def api_exercise_version(
    set_id: int, version_id: int, db: DBDep, course_id: ApiExerciseSetEditor
):
    """One version's status and problems, which `cocco push` polls until the
    build it queued has finished."""
    version = await query_exercise_version_summary(db, set_id, version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return JSONResponse(_exercise_version_json(set_id, version))
