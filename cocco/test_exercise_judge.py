"""The judge's side of exercises: claiming jobs, running them, building versions.

The unmarked tests need PostgreSQL and nothing else - the sandbox is replaced
where a job would reach it. The `integration` ones run real programs through
the harness in isolate, the way a classroom will.
"""

import asyncio
import contextlib
import textwrap
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from sqlalchemy import insert, select, text

import cocco.judge as cj
from cocco import model as cm
from cocco.repository import challenges, exercise_sets, judging as repo
from cocco.avatar import generate_identicon
from cocco.conftest import TEST_DATABASE
from cocco.exercises.compare import Rules
from cocco.model import (
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSessionMode,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
)
from cocco.schema import (
    EXERCISE_JOB_RESULT,
    ExerciseCheckResult,
    ExerciseExecution,
    ExerciseJobError,
    ExerciseRunResult,
    HarnessException,
)


SOURCE = textwrap.dedent(
    '''\
    ---
    title: Warm-up
    ---

    # Double

    Read a number and print its double.

    ```solution
    n = int(input("Number? "))
    print(n * 2)
    ```

    ```starter
    n = input()
    ```

    ```input
    21
    ```

    ```input hidden
    5
    ```

    # Divide

    Ten divided by the number read.

    ```solution
    a = int(input())
    print("before")
    print(10 // a)
    ```

    ```input
    0
    ```

    ```error
    ArithmeticError
    ```

    ```input
    2
    ```

    # Quick

    Print ok, and be quick about it.

    time: 1

    ```solution
    print("ok")
    ```
    '''
)

BROKEN = textwrap.dedent(
    '''\
    ---
    title: Warm-up
    ---

    # Double

    The solution is broken.

    ```solution
    raise RuntimeError("oops")
    ```

    ```input
    21
    ```
    '''
)


########################################################################
# helpers


def _user(conn, name: str = "s") -> int:
    image, image_mime = generate_identicon(name)
    return conn.execute(
        insert(cm.User)
        .values(email=f"{name}@unipd.it", nickname=name, image=image, image_mime=image_mime)
        .returning(cm.User.c.id)
    ).scalar_one()


def _set(db) -> tuple[int, int]:
    """A user, and an exercise set with no version yet: (user_id, set_id)."""
    with db.begin() as conn:
        user_id = _user(conn)
        course_id = conn.execute(
            insert(cm.Course).values(title="C", description="").returning(cm.Course.c.id)
        ).scalar_one()
        set_id = conn.execute(
            insert(ExerciseSet).values(course_id=course_id).returning(ExerciseSet.c.id)
        ).scalar_one()
    return user_id, set_id


def _version(db, set_id: int, source: str = SOURCE) -> int:
    """A `building` version, the way an import leaves one."""
    with db.begin() as conn:
        return conn.execute(
            insert(ExerciseSetVersion)
            .values(set_id=set_id, source=source, title="Warm-up")
            .returning(ExerciseSetVersion.c.id)
        ).scalar_one()


async def _enqueue(app_engine, kind: ExerciseJobKind, user_id: int, version_id: int, **job) -> int:
    """A job, through the web app's own insert - notification and all."""
    if kind is not ExerciseJobKind.build:
        job.setdefault("exercise_index", 1)
        job.setdefault("source", "")
    async with app_engine.connect() as conn:
        job_id = await exercise_sets.insert_exercise_job(
            conn, kind=kind, user_id=user_id, version_id=version_id, **job
        )
        await conn.commit()
    return job_id


def _job(db, job_id: int):
    with db.connect() as conn:
        return conn.execute(select(ExerciseJob).where(ExerciseJob.c.id == job_id)).one()


def _version_row(db, version_id: int):
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseSetVersion).where(ExerciseSetVersion.c.id == version_id)
        ).one()


def _current(db, set_id: int) -> int | None:
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseSet.c.current_version_id).where(ExerciseSet.c.id == set_id)
        ).scalar_one()


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


########################################################################
# the pure parts


def test_jsonb_safe_drops_nuls_and_lone_surrogates():
    assert cj._jsonb_safe({"a": ["x\x00y", "\ud800z"], "n": 1, "astral": "😀"}) == {
        "a": ["xy", "�z"],
        "n": 1,
        "astral": "😀",
    }


def test_the_report_is_read_off_the_last_line():
    report = (
        '{"stdout": "", "stderr": "", "truncated": false, "exit": 0, "exception": null}'
    )
    assert cj.parse_harness_report(f"junk\n{report}\n") is not None
    # Something written to fd 1 after the report is what is read, and is not one.
    assert cj.parse_harness_report(f"{report}\nforged?\n") is None
    assert cj.parse_harness_report("") is None


def test_a_report_carrying_a_nul_is_cleaned_as_it_is_read():
    report = cj.parse_harness_report(
        '{"stdout": "a\\u0000b\\ud800", "stderr": "", "truncated": false, '
        '"exit": 0, "exception": null}'
    )
    assert report is not None
    assert report.stdout == "ab�"


def _ok(stdout: str = "", exception: str | None = None, exit: int = 0) -> ExerciseExecution:
    mro = {
        "ZeroDivisionError": ["ZeroDivisionError", "ArithmeticError", "Exception", "BaseException", "object"],
        "ValueError": ["ValueError", "Exception", "BaseException", "object"],
    }
    return ExerciseExecution(
        status="ok",
        stdout=stdout,
        exit=exit,
        exception=None
        if exception is None
        else HarnessException(type=exception, classes=mro[exception], line=1, traceback=""),
    )


@pytest.mark.parametrize(
    "execution,expected_error,verdict",
    [
        (_ok("42\n"), None, "pass"),
        (_ok("41\n"), None, "fail"),
        (_ok("42\n", "ValueError"), None, "error"),
        (_ok("42\n", exit=3), None, "error"),
        # A subclass of the expected exception is the expected exception.
        (_ok("42\n", "ZeroDivisionError"), "ArithmeticError", "pass"),
        (_ok("42\n", "ValueError"), "ArithmeticError", "error"),
        (_ok("42\n"), "ArithmeticError", "noerror"),
        # The output before an expected exception is still compared.
        (_ok("41\n", "ZeroDivisionError"), "ZeroDivisionError", "fail"),
        (ExerciseExecution(status="ok", stdout="42\n", truncated=True), None, "truncated"),
        (ExerciseExecution(status="timeout"), None, "timeout"),
        (ExerciseExecution(status="no_report"), None, "no_report"),
    ],
)
def test_grade(execution, expected_error, verdict):
    assert cj.grade(execution, "42\n", expected_error, Rules())[0] == verdict


def test_a_report_nested_too_deep_is_no_report():
    """The last line is the program's to write, and a forgery nested deeper
    than the decoder follows is not a judge error."""
    assert cj.parse_harness_report("[" * 100_000 + "]" * 100_000) is None


def test_stored_output_is_clipped_and_says_so(monkeypatch):
    """`clipped` is the judge's cut and `truncated` stays the harness's."""
    monkeypatch.setattr(cj, "MAX_EXERCISE_STORED_OUTPUT", 4)
    stored = ExerciseRunResult(**cj._stored(ExerciseExecution(status="ok", stdout="123456")))
    assert (stored.stdout, stored.clipped, stored.truncated) == ("1234", True, False)


def test_a_result_round_trips_through_its_one_definition():
    """What the judge writes is what `EXERCISE_JOB_RESULT` reads back - the MRO
    under its wire name, `mro`, included."""
    result = ExerciseRunResult(**cj._stored(_ok("x\n", "ValueError")))
    dumped = result.model_dump(mode="json")
    assert dumped["exception"]["mro"][0] == "ValueError"
    assert EXERCISE_JOB_RESULT.validate_python(dumped) == result


########################################################################
# the queue


async def test_a_job_wakes_the_judge_when_it_commits(db, app_engine):
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    with psycopg.connect(f"postgresql:///{TEST_DATABASE}", autocommit=True) as listener:
        listener.execute(f"LISTEN {repo.QUEUE_CHANNEL}")
        await _enqueue(app_engine, ExerciseJobKind.build, user_id, version_id)
        [notification] = listener.notifies(timeout=2, stop_after=1)
    assert notification.channel == repo.QUEUE_CHANNEL


async def test_the_judge_takes_run_then_check_then_build_then_submission(
    db, app_engine, monkeypatch
):
    """Strict priority across the two tables: enqueued in the opposite order,
    each one older than the next, and taken most urgent first all the same."""
    taken = []

    async def run_job(engine, job):
        taken.append(job.kind.value)
        return ExerciseJobError(message="not run")

    async def build_job(engine, job):
        taken.append("build")
        return cj.VersionBuild(ExerciseVersionStatus.failed, [], None)

    async def grade(storage, run_data):
        taken.append("submission")
        return []

    monkeypatch.setattr(cj, "run_exercise_job", run_job)
    monkeypatch.setattr(cj, "build_job", build_job)
    monkeypatch.setattr(cj, "run_submission", grade)

    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    with db.begin() as conn:
        challenge_id = conn.execute(
            insert(cm.Challenge)
            .values(title="Hi", description="", max_memory_bytes=1 << 20, max_time_s=1.0)
            .returning(cm.Challenge.c.id)
        ).scalar_one()
    async with app_engine.connect() as conn:
        await challenges.insert_submission(
            conn, user_id=user_id, challenge_id=challenge_id,
            language="python", filename="a.py", source="",
        )
        await conn.commit()
    for kind in (ExerciseJobKind.build, ExerciseJobKind.check, ExerciseJobKind.run):
        await _enqueue(app_engine, kind, user_id, version_id)

    for _ in range(4):
        assert await cj.run_next(db, None)
    assert not await cj.run_next(db, None)
    assert taken == ["run", "check", "build", "submission"]


def test_a_claim_passes_over_a_job_another_claimer_holds(db):
    """`SKIP LOCKED`, as for submissions: the second claimer takes the next
    job rather than waiting for - and then taking again - the first one's."""
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    now = datetime.now(timezone.utc)
    with db.begin() as conn:
        oldest, second = (
            conn.execute(
                insert(ExerciseJob)
                .values(
                    kind=ExerciseJobKind.check,
                    user_id=user_id,
                    version_id=version_id,
                    exercise_index=index,
                    source="",
                    enqueued=now + timedelta(seconds=index),
                )
                .returning(ExerciseJob.c.id)
            ).scalar_one()
            for index in (1, 2)
        )

    with db.connect() as holder:
        holder.execute(
            select(ExerciseJob.c.id).where(ExerciseJob.c.id == oldest).with_for_update()
        )
        with db.connect() as conn:
            conn.execute(text("SET lock_timeout = '2s'"))
            claimed = repo.pull_queued_exercise_job(conn, ExerciseJobKind.check)
        holder.rollback()

    assert claimed is not None
    assert claimed.id == second


async def test_a_judge_stopped_mid_job_puts_it_back(db, app_engine, monkeypatch):
    running = asyncio.Event()

    async def run_job(engine, job):
        running.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cj, "run_exercise_job", run_job)
    user_id, set_id = _set(db)
    job_id = await _enqueue(app_engine, ExerciseJobKind.run, user_id, _version(db, set_id))

    task = asyncio.create_task(cj.run_exercise_from_queue(db, ExerciseJobKind.run))
    await asyncio.wait_for(running.wait(), 5)
    assert _job(db, job_id).status == ExerciseJobStatus.running

    await _cancel(task)
    job = _job(db, job_id)
    assert (job.status, job.started_at) == (ExerciseJobStatus.queued, None)


async def test_a_job_the_judge_fails_on_is_answered_not_left_running(
    db, app_engine, monkeypatch
):
    """Failure containment: the judge's exception becomes the job's result,
    and a build's failure fails its version, which would otherwise stay
    `building` with nobody left to finish it."""

    async def boom(engine, job):
        raise RuntimeError("isolate is on fire")

    monkeypatch.setattr(cj, "run_exercise_job", boom)
    monkeypatch.setattr(cj, "build_job", boom)
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    check_id = await _enqueue(app_engine, ExerciseJobKind.check, user_id, version_id)
    build_id = await _enqueue(app_engine, ExerciseJobKind.build, user_id, version_id)

    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.check)
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.build)

    for job_id in (check_id, build_id):
        job = _job(db, job_id)
        assert job.status == ExerciseJobStatus.done
        result = EXERCISE_JOB_RESULT.validate_python(job.result)
        assert isinstance(result, ExerciseJobError)
        assert "isolate is on fire" in result.message
    version = _version_row(db, version_id)
    assert version.status == ExerciseVersionStatus.failed
    assert [p["level"] for p in version.problems] == ["error"]
    assert _current(db, set_id) is None


async def test_a_check_on_an_exercise_the_version_lacks_is_an_error(db, app_engine):
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    job_id = await _enqueue(app_engine, ExerciseJobKind.check, user_id, version_id)
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.check)
    assert _job(db, job_id).result["kind"] == "error"


def test_a_version_is_written_once_and_the_set_only_moves_forward(db):
    """Two imports, two builds, finishing in the wrong order: the set ends on
    the newer one, and a second write of a built version changes nothing."""
    _, set_id = _set(db)
    older, newer = _version(db, set_id), _version(db, set_id)
    document = {"stand-in": True}
    ready = ExerciseVersionStatus.ready
    with db.begin() as conn:
        assert repo.finish_version_build(conn, newer, ready, [], document)
        assert repo.finish_version_build(conn, older, ready, [], document)
        assert not repo.finish_version_build(
            conn, newer, ExerciseVersionStatus.failed, [], None
        )
    assert _current(db, set_id) == newer
    assert _version_row(db, newer).status == ready


async def test_a_stopped_job_with_a_newer_one_queued_is_finished_not_requeued(
    db, app_engine, monkeypatch
):
    """The student pressed Run again while this one ran, and then the judge
    was stopped: the requeue would make two queued Runs for one button, which
    the unique index refuses. The stale one is finished instead, the newer one
    is left alone, and above all the cancellation still goes through - a
    worker that swallowed it would hang the judge on SIGTERM."""
    running = asyncio.Event()

    async def run_job(engine, job):
        running.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cj, "run_exercise_job", run_job)
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    stale = await _enqueue(app_engine, ExerciseJobKind.run, user_id, version_id)

    task = asyncio.create_task(cj.run_exercise_from_queue(db, ExerciseJobKind.run))
    await asyncio.wait_for(running.wait(), 5)
    newer = await _enqueue(app_engine, ExerciseJobKind.run, user_id, version_id)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert task.cancelled()

    job = _job(db, stale)
    assert job.status == ExerciseJobStatus.done
    assert isinstance(EXERCISE_JOB_RESULT.validate_python(job.result), ExerciseJobError)
    assert _job(db, newer).status == ExerciseJobStatus.queued


async def test_a_failing_requeue_does_not_swallow_the_cancellation(
    db, app_engine, monkeypatch
):
    running = asyncio.Event()

    async def run_job(engine, job):
        running.set()
        await asyncio.Event().wait()

    def requeue(conn, job_id, superseded):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(cj, "run_exercise_job", run_job)
    monkeypatch.setattr(repo, "requeue_exercise_job", requeue)
    user_id, set_id = _set(db)
    await _enqueue(app_engine, ExerciseJobKind.run, user_id, _version(db, set_id))

    task = asyncio.create_task(cj.run_exercise_from_queue(db, ExerciseJobKind.run))
    await asyncio.wait_for(running.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


def test_old_runs_and_previews_are_pruned_and_nothing_else(db):
    """A run counts towards nothing, and neither does a staff preview's check
    (no session). A session's check is progress, and a build is its version's
    record: both are kept, however old."""
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    long_ago = datetime.now(timezone.utc) - timedelta(days=200)
    jobs = {}
    with db.begin() as conn:
        session_id = conn.execute(
            insert(ExerciseSession)
            .values(
                set_id=set_id,
                version_id=version_id,
                label="Lab A",
                mode=ExerciseSessionMode.live,
                unlocked_through=1,
            )
            .returning(ExerciseSession.c.id)
        ).scalar_one()
        for name, kind, session, enqueued in [
            ("old run", ExerciseJobKind.run, session_id, long_ago),
            ("old preview run", ExerciseJobKind.run, None, long_ago),
            ("old preview check", ExerciseJobKind.check, None, long_ago),
            ("old check", ExerciseJobKind.check, session_id, long_ago),
            ("old build", ExerciseJobKind.build, None, long_ago),
            ("new run", ExerciseJobKind.run, session_id, datetime.now(timezone.utc)),
            ("new preview check", ExerciseJobKind.check, None, datetime.now(timezone.utc)),
        ]:
            build = kind is ExerciseJobKind.build
            jobs[name] = conn.execute(
                insert(ExerciseJob)
                .values(
                    kind=kind,
                    status=ExerciseJobStatus.done,
                    user_id=user_id,
                    version_id=version_id,
                    session_id=session,
                    exercise_index=None if build else 1,
                    source=None if build else "",
                    result={"kind": "error", "message": ""},
                    enqueued=enqueued,
                    started_at=enqueued,
                    finished_at=enqueued,
                )
                .returning(ExerciseJob.c.id)
            ).scalar_one()

    with db.begin() as conn:
        assert repo.prune_run_jobs(conn, 10) == 3
    with db.connect() as conn:
        left = set(conn.execute(select(ExerciseJob.c.id)).scalars())
    assert left == {
        jobs[name] for name in ("old check", "old build", "new run", "new preview check")
    }


def test_pruning_goes_in_batches(db, monkeypatch):
    user_id, set_id = _set(db)
    version_id = _version(db, set_id)
    long_ago = datetime.now(timezone.utc) - timedelta(days=200)
    with db.begin() as conn:
        conn.execute(
            insert(ExerciseJob),
            [
                dict(
                    kind=ExerciseJobKind.run,
                    status=ExerciseJobStatus.done,
                    user_id=user_id,
                    version_id=version_id,
                    exercise_index=1,
                    source="",
                    result={"kind": "error", "message": ""},
                    enqueued=long_ago,
                    started_at=long_ago,
                    finished_at=long_ago,
                )
                for _ in range(5)
            ],
        )
    monkeypatch.setattr(cj, "PRUNE_BATCH", 2)
    assert cj._prune(db) == 5


async def test_pruning_is_rate_limited(db, monkeypatch):
    calls = []
    monkeypatch.setattr(cj, "_prune", lambda engine: calls.append(1) or 0)
    monkeypatch.setattr(cj, "_next_prune", 0.0)
    await cj.prune_if_due(db)
    await cj.prune_if_due(db)
    assert calls == [1]


########################################################################
# real programs, in isolate


async def _build(db, app_engine, user_id: int, set_id: int, source: str) -> tuple[int, int]:
    """Import `source` into the set and build it: (version_id, build job id)."""
    version_id = _version(db, set_id, textwrap.dedent(source))
    job_id = await _enqueue(app_engine, ExerciseJobKind.build, user_id, version_id)
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.build)
    return version_id, job_id


async def _built(db, app_engine) -> tuple[int, int, int]:
    """`SOURCE`, imported and built: (user_id, set_id, version_id)."""
    user_id, set_id = _set(db)
    version_id, _ = await _build(db, app_engine, user_id, set_id, SOURCE)
    version = _version_row(db, version_id)
    assert version.status == ExerciseVersionStatus.ready, version.problems
    return user_id, set_id, version_id


async def _check(db, app_engine, user_id, version_id, index, source) -> ExerciseCheckResult:
    job_id = await _enqueue(
        app_engine, ExerciseJobKind.check, user_id, version_id,
        exercise_index=index, source=textwrap.dedent(source),
    )
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.check)
    result = EXERCISE_JOB_RESULT.validate_python(_job(db, job_id).result)
    assert isinstance(result, ExerciseCheckResult), result
    return result


async def _run(db, app_engine, user_id, version_id, source, stdin) -> ExerciseRunResult:
    job_id = await _enqueue(
        app_engine, ExerciseJobKind.run, user_id, version_id,
        source=textwrap.dedent(source), stdin=stdin,
    )
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.run)
    result = EXERCISE_JOB_RESULT.validate_python(_job(db, job_id).result)
    assert isinstance(result, ExerciseRunResult), result
    return result


@pytest.mark.integration
async def test_a_build_computes_the_expected_outputs_and_becomes_current(db, app_engine):
    user_id, set_id = _set(db)
    version_id, job_id = await _build(db, app_engine, user_id, set_id, SOURCE)
    assert _current(db, set_id) == version_id
    version = _version_row(db, version_id)
    double, divide, _ = version.document["exercises"]
    # Computed in check mode, so the solution's prompt is not part of it.
    assert [t["expected_output"] for t in double["tests"]] == ["42\n", "10\n"]
    assert [(t["expected_output"], t["expected_error"]) for t in divide["tests"]] == [
        ("before\n", "ArithmeticError"),
        ("before\n5\n", None),
    ]
    assert version.problems == []
    assert _job(db, job_id).result == {
        "kind": "build", "status": "ready", "errors": 0, "warnings": 0, "written": True,
    }


@pytest.mark.integration
async def test_prompts_are_dropped_in_a_check_and_echoed_in_a_run(db, app_engine):
    user_id, _, version_id = await _built(db, app_engine)

    # A different prompt from the solution's, and it does not matter.
    result = await _check(
        db, app_engine, user_id, version_id, 1,
        'print(int(input("Give me a number: ")) * 2)\n',
    )
    assert result.passed
    assert [t.verdict for t in result.tests] == ["pass", "pass"]
    # Stored, hidden or not, and marked so the pages can tell.
    assert [t.hidden for t in result.tests] == [False, True]
    assert result.tests[1].stdout == "10\n"

    run = await _run(
        db, app_engine, user_id, version_id,
        'n = int(input("Number? "))\nprint(n * 2)\n', "21\n",
    )
    assert run.status == "ok"
    assert run.stdout == "Number? 21\n42\n"


@pytest.mark.integration
async def test_an_expected_exception_matches_its_subclasses(db, app_engine):
    user_id, _, version_id = await _built(db, app_engine)

    divides = await _check(
        db, app_engine, user_id, version_id, 2,
        """\
        a = int(input())
        print("before")
        print(10 // a)
        """,
    )
    assert divides.passed
    assert divides.tests[0].exception.type == "ZeroDivisionError"

    wrong_error = await _check(
        db, app_engine, user_id, version_id, 2,
        """\
        a = int(input())
        print("before")
        print(10 // a if a else int("x"))
        """,
    )
    assert [t.verdict for t in wrong_error.tests] == ["error", "pass"]
    assert wrong_error.tests[0].exception.type == "ValueError"
    assert wrong_error.tests[0].exception.line == 3

    no_error = await _check(
        db, app_engine, user_id, version_id, 2,
        """\
        a = int(input())
        print("before")
        print(10 // a if a else 0)
        """,
    )
    assert [t.verdict for t in no_error.tests] == ["noerror", "pass"]


@pytest.mark.integration
async def test_a_program_that_never_ends_times_out(db, app_engine):
    user_id, _, version_id = await _built(db, app_engine)
    result = await _check(db, app_engine, user_id, version_id, 3, "while True:\n    pass\n")
    assert [t.verdict for t in result.tests] == ["timeout"]
    assert not result.passed


@pytest.mark.integration
async def test_an_endless_print_stops_at_the_cap(db, app_engine):
    user_id, _, version_id = await _built(db, app_engine)
    result = await _check(db, app_engine, user_id, version_id, 3, 'while True:\n    print("x")\n')
    [test] = result.tests
    assert test.verdict == "truncated"
    assert test.truncated and test.clipped
    assert len(test.stdout) == cj.MAX_EXERCISE_STORED_OUTPUT


@pytest.mark.integration
async def test_a_program_that_ends_before_the_report_is_its_own_doing(db, app_engine):
    user_id, _, version_id = await _built(db, app_engine)
    result = await _check(
        db, app_engine, user_id, version_id, 3, 'import os\nprint("ok")\nos._exit(0)\n'
    )
    assert [t.verdict for t in result.tests] == ["no_report"]

    run = await _run(db, app_engine, user_id, version_id, "import os\nos._exit(3)\n", "")
    assert run.status == "no_report"


@pytest.mark.integration
async def test_a_program_that_sleeps_runs_into_the_wall_clock(db, app_engine):
    """`--time` is CPU time, which a sleeping program does not use: without the
    wall-clock limit this would hold a box and a worker for ever."""
    user_id, _, version_id = await _built(db, app_engine)
    start = asyncio.get_running_loop().time()
    result = await _check(
        db, app_engine, user_id, version_id, 3, "import time\ntime.sleep(10**9)\n"
    )
    assert [t.verdict for t in result.tests] == ["timeout"]
    # Exercise 3 has `time: 1`, so the wall clock is 3 s.
    assert asyncio.get_running_loop().time() - start < 10


@pytest.mark.integration
async def test_a_flood_written_past_the_harness_is_stopped_by_the_file_size(
    db, app_engine
):
    """Writing to fd 1 directly bypasses the harness's cap, and the box's
    stdout is read whole: `--fsize` is what bounds it, and the program then
    ends without reporting."""
    user_id, _, version_id = await _built(db, app_engine)
    run = await _run(
        db, app_engine, user_id, version_id,
        'import os\nwhile True:\n    os.write(1, b"x" * 65536)\n', "",
    )
    assert run.status == "no_report"
    assert run.clipped
    assert len(run.stdout) == cj.MAX_EXERCISE_STORED_OUTPUT


@pytest.mark.integration
async def test_a_bare_word_argument_is_not_copied_from_the_working_directory(
    db, app_engine, tmp_path, monkeypatch
):
    """Under systemd the judge runs in `/`, where `run` is a directory full of
    credentials. The harness's mode is a bare word, and must stay one."""
    for name in ("run", "check"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "secret").write_text("do not copy me")
    monkeypatch.chdir(tmp_path)
    user_id, _, version_id = await _built(db, app_engine)
    run = await _run(
        db, app_engine, user_id, version_id,
        "import os\nprint(sorted(os.listdir('.')))\n", "",
    )
    assert run.stdout == "['_stderr', '_stdout', 'harness.py', 'main.py']\n"

    python = await cj.resolve_runtime("python3", "python")
    res = await cj.isolate_run(
        python,
        None,
        ["-c", "import os, sys; print(sorted(os.listdir('.')), sys.argv[1:])", "run"],
    )
    assert res.stdout == "['_stderr', '_stdout'] ['run']\n"


@pytest.mark.integration
async def test_set_order_is_the_same_in_every_box(db, app_engine):
    """`PYTHONHASHSEED=0`: the order a set prints in is the solution's and the
    student's alike, rather than one run's luck."""
    user_id, _, version_id = await _built(db, app_engine)
    program = "import os\nprint(os.environ.get('PYTHONHASHSEED'))\nprint(list(set('abcdefghij')))\n"
    first = await _run(db, app_engine, user_id, version_id, program, "")
    second = await _run(db, app_engine, user_id, version_id, program, "")
    assert first.stdout.startswith("0\n")
    assert first.stdout == second.stdout


@pytest.mark.integration
async def test_what_jsonb_cannot_hold_is_dropped_before_it_gets_there(db, app_engine):
    user_id, _, version_id = await _built(db, app_engine)
    run = await _run(db, app_engine, user_id, version_id, 'print("a\\0b\\ud800")\n', "")
    assert run.stdout == "ab�\n"


@pytest.mark.integration
async def test_a_failed_build_leaves_the_current_version_where_it_was(db, app_engine):
    user_id, set_id, ready = await _built(db, app_engine)
    broken = _version(db, set_id, BROKEN)
    job_id = await _enqueue(app_engine, ExerciseJobKind.build, user_id, broken)
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.build)

    version = _version_row(db, broken)
    assert version.status == ExerciseVersionStatus.failed
    assert version.document is None
    [problem] = version.problems
    assert (problem["level"], problem["exercise"], problem["test"]) == ("error", 1, 1)
    assert problem["text"] == (
        "The solution ends with an error on test 1. If that is intended, "
        'add an "error" block with RuntimeError after the input.'
    )
    assert "RuntimeError" in problem["detail"]
    assert _current(db, set_id) == ready
    assert _job(db, job_id).result["status"] == "failed"


@pytest.mark.integration
async def test_a_starter_that_already_passes_is_a_warning(db, app_engine):
    user_id, set_id = _set(db)
    version_id = _version(
        db,
        set_id,
        textwrap.dedent(
            '''\
            ---
            title: Too easy
            ---

            # Hello

            Say hi.

            ```solution
            print("hi")
            ```

            ```starter
            print("hi")
            ```
            '''
        ),
    )
    await _enqueue(app_engine, ExerciseJobKind.build, user_id, version_id)
    assert await cj.run_exercise_from_queue(db, ExerciseJobKind.build)
    version = _version_row(db, version_id)
    assert version.status == ExerciseVersionStatus.ready
    assert [p["text"] for p in version.problems] == [
        "The starter code already passes every test."
    ]


@pytest.mark.integration
async def test_what_a_build_warns_about_and_resolves(db, app_engine):
    """The author-page problems that need Python: a solution that prints
    something random, an `output` block the solution disagrees with - which
    is used all the same - and an empty `error` block, which becomes whatever
    the solution raises."""
    user_id, set_id = _set(db)
    version_id, _ = await _build(
        db, app_engine, user_id, set_id,
        """\
        ---
        title: Warnings
        ---

        # Random

        Print a random number.

        ```solution
        import random
        print(random.random())
        ```

        # Disagreeing

        Print 42.

        ```solution
        print(42)
        ```

        ```input
        ```

        ```output
        43
        ```

        # Raising

        Divide by zero.

        ```solution
        print(1 // 0)
        ```

        ```input
        ```

        ```error
        ```
        """,
    )
    version = _version_row(db, version_id)
    assert version.status == ExerciseVersionStatus.ready, version.problems
    assert [(p["exercise"], p["level"], p["text"]) for p in version.problems] == [
        (
            1,
            "warning",
            "The solution gives different outputs in two runs of test 1 "
            "(random numbers?): students may not be able to pass it.",
        ),
        (
            2,
            "warning",
            'On test 1 the output written in the "output" block differs from '
            'the solution\'s. Using the "output" block.',
        ),
    ]
    _, disagreeing, raising = version.document["exercises"]
    assert disagreeing["tests"][0]["expected_output"] == "43\n"
    assert raising["tests"][0]["expected_error"] == "ZeroDivisionError"
