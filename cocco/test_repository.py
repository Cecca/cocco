from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

import cocco.model as cm
from cocco.avatar import generate_identicon
from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    Submission,
    TestCase,
    TestOutcome,
    User,
    metadata,
)
from cocco.model import LeaderboardRanking, UserRole
from cocco.auth import get_or_create_user
from cocco.repository import (
    challenge_in_course,
    count_running_submissions,
    count_testcase_outcomes,
    insert_challenge,
    insert_course,
    insert_enrollment,
    insert_user,
    pull_queued_submission,
    query_challenge,
    query_challenge_leaderboard,
    query_challenge_ranking,
    query_challenge_standings,
    query_challenge_testcases,
    query_course_challenges,
    query_course_info,
    query_course_role,
    query_ordered_challenge_ids,
    query_sample_testcase,
    query_submission,
    query_submission_outcomes,
    query_user_by_email,
    query_user_role,
    query_user_submissions,
    regrade_challenge,
    reorder_challenges,
    user_is_admin,
)
from cocco.schema import SubmissionRunData


def _make_engine(db_url="sqlite:///:memory:"):
    engine = create_engine(db_url)
    metadata.create_all(engine)
    return engine


def _make_example_database(db_url="sqlite:///:memory:"):
    engine = _make_engine(db_url)
    now = datetime.now()

    with engine.begin() as conn:
        users = [
            ("root@example.com", "root"),
            ("alice@example.com", "alice"),
            ("bob@example.com", "bob"),
            ("carol@example.com", "carol"),
            ("dave@example.com", "dave"),
            ("erin@example.com", "erin"),
        ]
        user_ids = {}
        for email, nickname in users:
            image, image_mime = generate_identicon(nickname)
            user_ids[nickname] = conn.execute(
                insert(User)
                .values(
                    email=email,
                    nickname=nickname,
                    image=image,
                    image_mime=image_mime,
                )
                .returning(User.c.id)
            ).scalar_one()

        courses = [
            ("Algorithms 101", "intro to algorithms"),
            ("Data Structures", "trees, graphs, hashing"),
            ("Systems Programming", "C, memory, syscalls"),
        ]
        course_ids = {
            title: conn.execute(
                insert(Course)
                .values(title=title, description=description)
                .returning(Course.c.id)
            ).scalar_one()
            for title, description in courses
        }

        enrollments = [
            ("alice", "Algorithms 101", cm.UserRole.teacher),
            ("alice", "Data Structures", cm.UserRole.teacher),
            ("bob", "Systems Programming", cm.UserRole.teacher),
            ("carol", "Algorithms 101", cm.UserRole.student),
            ("carol", "Data Structures", cm.UserRole.student),
            ("dave", "Algorithms 101", cm.UserRole.student),
            ("dave", "Systems Programming", cm.UserRole.student),
            ("erin", "Systems Programming", cm.UserRole.student),
        ]
        for nickname, course_title, role in enrollments:
            conn.execute(
                insert(Enrollment).values(
                    user_id=user_ids[nickname],
                    course_id=course_ids[course_title],
                    role=role,
                )
            )

        challenges = [
            ("Hello", "say hi", "Algorithms 101", 1 << 20, 1.0, 1),
            ("Sum", "sum two numbers", "Algorithms 101", 1 << 20, 1.0, 1),
            ("BST insert", "insert into a BST", "Data Structures", 1 << 21, 2.0, 1),
            ("Buffer copy", "copy bytes via syscalls", "Systems Programming", 1 << 22, 2.0, 1),
            ("Parallel sum", "sum with threads", "Systems Programming", 1 << 22, 3.0, 4),
        ]
        challenge_ids = {
            title: conn.execute(
                insert(Challenge)
                .values(
                    title=title,
                    description=description,
                    course_id=course_ids[course_title],
                    max_memory_bytes=mem,
                    max_time_s=time_s,
                    max_threads=threads,
                )
                .returning(Challenge.c.id)
            ).scalar_one()
            for title, description, course_title, mem, time_s, threads in challenges
        }

        testcase_ids: dict[str, list[int]] = {}
        for challenge_title in challenge_ids:
            ids = []
            for order in range(3):
                tc_id = conn.execute(
                    insert(TestCase)
                    .values(
                        challenge_id=challenge_ids[challenge_title],
                        execution_order=order,
                        input_path=f"{challenge_title}/in_{order}.txt",
                        expected_output_path=f"{challenge_title}/out_{order}.txt",
                        # the last one is public but retired, so the "retired
                        # wins over public" rule is exercised
                        is_sample=order in (0, 2),
                        is_active=order != 2,
                    )
                    .returning(TestCase.c.id)
                ).scalar_one()
                ids.append(tc_id)
            testcase_ids[challenge_title] = ids

        submissions = [
            ("carol", "Hello", "python", "print('hi')",
             cm.SubmissionStatus.done, now - timedelta(minutes=30)),
            ("carol", "Hello", "c", "print('there')",
             cm.SubmissionStatus.done, now - timedelta(minutes=35)),
            ("carol", "Sum", "python", "a,b=map(int,input().split());print(a+b)",
             cm.SubmissionStatus.done, now - timedelta(minutes=20)),
            ("carol", "BST insert", "cpp", "// not finished",
             cm.SubmissionStatus.queued, now - timedelta(minutes=2)),
            ("dave", "Hello", "c", "// broken", cm.SubmissionStatus.done,
             now - timedelta(minutes=25)),
            ("dave", "Buffer copy", "c", "int main(){return 0;}",
             cm.SubmissionStatus.running, now - timedelta(minutes=5)),
            ("dave", "Parallel sum", "cpp", "// pending",
             cm.SubmissionStatus.queued, now - timedelta(minutes=1)),
            ("erin", "Buffer copy", "c", "int main(){return 0;}",
             cm.SubmissionStatus.queued, now - timedelta(seconds=30)),
            ("erin", "Parallel sum", "cpp", "// pending",
             cm.SubmissionStatus.queued, now - timedelta(seconds=10)),
        ]
        submission_ids = []
        for nickname, challenge_title, language, source, status, enqueued in submissions:
            sub_id = conn.execute(
                insert(Submission)
                .values(
                    user_id=user_ids[nickname],
                    challenge_id=challenge_ids[challenge_title],
                    language=language,
                    filename=f"solution.{language}",
                    source=source,
                    status=status,
                    enqueued=enqueued,
                )
                .returning(Submission.c.id)
            ).scalar_one()
            submission_ids.append((sub_id, challenge_title, status))

        outcome_cycle = [
            (cm.OutcomeEnum.success, 1.0),
            (cm.OutcomeEnum.success, 1.0),
            (cm.OutcomeEnum.runtime_error, 0.0),
            (cm.OutcomeEnum.timeout, 0.0),
            (cm.OutcomeEnum.compile_failure, 0.0),
        ]
        cursor = 0
        for sub_id, challenge_title, status in submission_ids:
            if status != cm.SubmissionStatus.done:
                continue
            for tc_id in testcase_ids[challenge_title]:
                outcome, score = outcome_cycle[cursor % len(outcome_cycle)]
                cursor += 1
                conn.execute(
                    insert(TestOutcome).values(
                        submission_id=sub_id,
                        testcase_id=tc_id,
                        outcome=outcome,
                        elapsed_s=0.05 * (cursor + 1),
                        peak_memory_bytes=512 * 1024 * (cursor + 1),
                        score=score,
                        message="ok" if outcome == cm.OutcomeEnum.success else "fail",
                        stdout="hello\n" if outcome == cm.OutcomeEnum.success else "",
                        stderr="" if outcome == cm.OutcomeEnum.success else "boom",
                    )
                )

    return engine


async def test_get_queued_submission_returns_oldest_queued():
    engine = _make_engine()
    now = datetime.now()

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@example.com",
                nickname="the-admin",
                image=image,
                image_mime=image_mime,
            )
            .returning(User.c.id)
        ).scalar_one()
        challenge_id = conn.execute(
            insert(Challenge)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
            )
            .returning(Challenge.c.id)
        ).scalar_one()

        oldest_id = conn.execute(
            insert(Submission)
            .values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
                enqueued=now - timedelta(seconds=10),
            )
            .returning(Submission.c.id)
        ).scalar_one()

        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('b')",
                status=cm.SubmissionStatus.queued,
                enqueued=now,
            )
        )
        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('c')",
                status=cm.SubmissionStatus.running,
                enqueued=now - timedelta(seconds=20),
            )
        )

    with engine.connect() as conn:
        result: SubmissionRunData | None = await pull_queued_submission(conn)

    assert result is not None
    assert result.submission_id == oldest_id
    # No evaluator of its own, so the judge falls back to the bundled default.
    assert result.evaluator_path is None


async def test_queued_submission_carries_the_challenge_evaluator():
    """The judge learns which checker to grade with from the run data, and
    from nowhere else."""
    engine = _make_engine()

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@example.com",
                nickname="the-admin",
                image=image,
                image_mime=image_mime,
            )
            .returning(User.c.id)
        ).scalar_one()
        challenge_id = conn.execute(
            insert(Challenge)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
                evaluator_path="evaluator_1/checker.py",
            )
            .returning(Challenge.c.id)
        ).scalar_one()
        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
            )
        )

    with engine.connect() as conn:
        result: SubmissionRunData | None = await pull_queued_submission(conn)

    assert result is not None
    assert result.evaluator_path == "evaluator_1/checker.py"


async def test_queued_submission_skips_retired_testcases():
    """Retiring a test case is what stops it grading new submissions."""
    engine = _make_engine()

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@example.com",
                nickname="the-admin",
                image=image,
                image_mime=image_mime,
            )
            .returning(User.c.id)
        ).scalar_one()
        challenge_id = conn.execute(
            insert(Challenge)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
            )
            .returning(Challenge.c.id)
        ).scalar_one()

        for order, is_active in enumerate([True, False, True]):
            conn.execute(
                insert(TestCase).values(
                    challenge_id=challenge_id,
                    execution_order=order,
                    input_path=f"in_{order}.txt",
                    expected_output_path=f"out_{order}.txt",
                    is_active=is_active,
                )
            )

        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
                enqueued=datetime.now(),
            )
        )

    with engine.connect() as conn:
        result = await pull_queued_submission(conn)

    assert result is not None
    assert [t.execution_order for t in result.test_cases] == [0, 2]


async def test_get_queued_submission_returns_none_when_empty():
    engine = _make_engine()
    with engine.connect() as conn:
        result = await pull_queued_submission(conn)
    assert result is None


def _postgres_url():
    import os

    return (
        f"postgresql+psycopg://{os.environ['PGUSER']}@/"
        f"{os.environ['PGDATABASE']}?host={os.environ['PGHOST']}"
    )


# The json_agg / row_to_json queries backing the web pages are Postgres-only,
# so this needs a live server (see PGUSER/PGDATABASE/PGHOST in the devShell).
@pytest.mark.integration
async def test_challenge_page_queries():
    url = _postgres_url()
    sync_engine = create_engine(url)
    metadata.drop_all(sync_engine)
    _make_example_database(url)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        # carol (user 4) is a student on Algorithms 101
        challenge = await query_challenge(conn, user_id=4, challenge_id=1)
        assert challenge is not None
        assert challenge["title"] == "Hello"
        assert challenge["course_title"] == "Algorithms 101"
        assert challenge["number"] == 1
        assert challenge["viewer_role"] == "student"
        # Two of the three are marked public, but the third is retired, and a
        # retired case is not offered to students however it is flagged.
        assert [s["execution_order"] for s in challenge["samples"]] == [0]

        # alice (user 2) teaches it, and is the one who gets the Edit button
        as_teacher = await query_challenge(conn, user_id=2, challenge_id=1)
        assert as_teacher is not None
        assert as_teacher["viewer_role"] == "teacher"

        # carol (user 4) has two attempts on "Hello", newest first
        submissions = await query_user_submissions(conn, user_id=4, challenge_id=1)
        assert submissions is not None
        assert [s["attempt"] for s in submissions] == [2, 1]
        assert [s["filename"] for s in submissions] == ["solution.python", "solution.c"]
        assert sum(1 for s in submissions if s["is_best"]) == 1

        # a challenge nobody has attempted still comes back, with no samples lost
        untouched = await query_challenge(conn, user_id=4, challenge_id=5)
        assert untouched is not None
        assert untouched["title"] == "Parallel sum"
        # carol is not enrolled on Systems Programming
        assert untouched["viewer_role"] is None

        # ...and its submission list is simply empty rather than None-ish
        assert await query_user_submissions(conn, user_id=4, challenge_id=5) is None

        # The editor's authorization check, and the list it drives
        assert await query_user_role(conn, user_id=2, challenge_id=1) == UserRole.teacher
        assert await query_user_role(conn, user_id=4, challenge_id=1) == UserRole.student
        assert await query_user_role(conn, user_id=4, challenge_id=5) is None

        # The editor lists retired cases too - that is how one is reinstated
        testcases = await query_challenge_testcases(conn, 1)
        assert [tc.execution_order for tc in testcases] == [0, 1, 2]
        assert [tc.is_sample for tc in testcases] == [True, False, True]
        assert [tc.is_active for tc in testcases] == [True, True, False]

        # ...but the retired one is not downloadable despite is_sample
        assert await query_sample_testcase(conn, 1, testcases[0].id) is not None
        assert await query_sample_testcase(conn, 1, testcases[2].id) is None

        # Every "Hello" submission was graded on all three, so none of them can
        # be deleted; a case on an unattempted challenge still can.
        assert [tc.n_outcomes for tc in testcases] == [3, 3, 3]
        assert await count_testcase_outcomes(conn, testcases[0].id) == 3

        untouched_cases = await query_challenge_testcases(conn, 5)
        assert [tc.n_outcomes for tc in untouched_cases] == [0, 0, 0]
        assert await count_testcase_outcomes(conn, untouched_cases[0].id) == 0

        # ── the staff view ────────────────────────────────────────────────
        # One row per student, best attempt first. alice only teaches the
        # course, so she is not part of the cohort; carol and dave are.
        standings = await query_challenge_standings(conn, 1)
        assert standings is not None
        assert [s["nickname"] for s in standings] == ["carol", "dave"]
        carol, dave = standings
        assert [carol["rank"], dave["rank"]] == [1, 2]
        # Both peak at 2 of 3 tests; carol is ahead on the tie-break, time.
        assert carol["score"] == pytest.approx(2 / 3)
        assert dave["score"] == pytest.approx(2 / 3)
        assert carol["elapsed_s"] < dave["elapsed_s"]
        assert [carol["n_passed"], carol["n_tests"]] == [2, 3]
        # Two attempts, but the row describes the better of them
        assert [carol["n_attempts"], dave["n_attempts"]] == [2, 1]
        assert [carol["n_pending"], dave["n_pending"]] == [0, 0]

        # An enrolled student who has handed in nothing still gets a row: who is
        # missing is the first thing a teacher looks for.
        sum_board = await query_challenge_standings(conn, 2)
        assert sum_board is not None
        idle = next(s for s in sum_board if s["nickname"] == "dave")
        assert idle["n_attempts"] == 0
        assert idle["score"] is None
        assert idle["last_enqueued"] is None

        # A queued attempt is pending and keeps a *null* score, rather than
        # being ranked as a zero it has not earned
        pending = await query_challenge_standings(conn, 5)
        assert pending is not None
        assert sorted(s["nickname"] for s in pending) == ["dave", "erin"]
        assert all(s["n_pending"] == 1 and s["score"] is None for s in pending)

        # ── the leaderboard ───────────────────────────────────────────────
        # A challenge ranks by running time until someone changes it
        assert (
            await query_challenge_ranking(conn, 1) == LeaderboardRanking.running_time
        )
        assert await query_challenge_ranking(conn, 999) is None

        # Nobody passed all three tests of "Hello", so the speed board is empty
        # even though there are graded submissions to show...
        assert (
            await query_challenge_leaderboard(
                conn, 1, LeaderboardRanking.running_time
            )
            is None
        )

        # ...and that is exactly what the score boards are for: carol and dave
        # both peak at 2 of 3, and the tie goes to the faster one.
        by_total = await query_challenge_leaderboard(
            conn, 1, LeaderboardRanking.total_score
        )
        assert by_total is not None
        assert [e["nickname"] for e in by_total] == ["carol", "dave"]
        assert [e["rank"] for e in by_total] == [1, 2]
        assert by_total[0]["total_score"] == pytest.approx(2.0)
        # The denominator the points are shown out of
        assert [e["n_tests"] for e in by_total] == [3, 3]
        assert by_total[0]["elapsed_s"] < by_total[1]["elapsed_s"]

        by_average = await query_challenge_leaderboard(
            conn, 1, LeaderboardRanking.average_score
        )
        assert by_average is not None
        assert [e["nickname"] for e in by_average] == ["carol", "dave"]
        assert by_average[0]["average_score"] == pytest.approx(2 / 3)

        # A user is represented once, by their best attempt: carol's other
        # "Hello" submission scored 1 of 3 and is nowhere to be seen.
        assert len([e for e in by_total if e["nickname"] == "carol"]) == 1

        # ── regrading ─────────────────────────────────────────────────────
        # Nothing is running on "Hello", but dave's "Buffer copy" is - that is
        # what makes the route refuse.
        assert await count_running_submissions(conn, 1) == 0
        assert await count_running_submissions(conn, 4) == 1

        # Rewrites the fixture, so it goes last. The connection is never
        # committed: like the other mutations, this one leaves that to the
        # caller, and the assertions below read it back inside the transaction.
        assert await regrade_challenge(conn, 1) == 3

        statuses = (
            await conn.execute(
                select(Submission.c.status).where(Submission.c.challenge_id == 1)
            )
        ).scalars().all()
        assert set(statuses) == {cm.SubmissionStatus.queued}
        assert [tc.n_outcomes for tc in await query_challenge_testcases(conn, 1)] == [
            0, 0, 0,
        ]

        # "Sum" is on the same course and shares nothing else: a missing
        # challenge_id filter would have wiped it too.
        assert [tc.n_outcomes for tc in await query_challenge_testcases(conn, 2)] == [
            1, 1, 1,
        ]
        assert (
            await conn.execute(
                select(Submission.c.status).where(Submission.c.challenge_id == 2)
            )
        ).scalars().all() == [cm.SubmissionStatus.done]
    await engine.dispose()


@pytest.mark.integration
async def test_submission_page_queries():
    """The two queries behind the single-submission page.

    Submission ids follow the order of the fixture's `submissions` list, so 1
    and 2 are carol's two attempts at "Hello" - the older one (2) being the one
    the fixture's outcome cycle gives a compile failure.
    """
    url = _postgres_url()
    sync_engine = create_engine(url)
    metadata.drop_all(sync_engine)
    _make_example_database(url)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        newer = await query_submission(conn, 1)
        assert newer is not None
        # The whole document the page renders from: the file, who wrote it, and
        # where it belongs
        assert newer["source"] == "print('hi')"
        assert newer["language"] == "python"
        assert newer["filename"] == "solution.python"
        assert newer["author_nickname"] == "carol"
        assert newer["challenge_title"] == "Hello"
        assert newer["course_title"] == "Algorithms 101"
        # Carol's second attempt: the numbering matches query_user_submissions,
        # which is what the table the viewer clicked showed them
        assert newer["attempt"] == 2
        history = await query_user_submissions(conn, user_id=4, challenge_id=1)
        assert history is not None
        assert [(s["id"], s["attempt"]) for s in history] == [(1, 2), (2, 1)]

        # Two of "Hello"'s three test cases passed - the same aggregate the
        # standings and the history compute
        assert [newer["n_passed"], newer["n_tests"]] == [2, 3]
        assert newer["score"] == pytest.approx(2 / 3)

        # In execution order, carrying what the page needs to decide whether a
        # student may see the free text of each one
        outcomes = newer["outcomes"]
        assert [o["execution_order"] for o in outcomes] == [0, 1, 2]
        assert [o["is_sample"] for o in outcomes] == [True, False, True]
        assert [o["is_active"] for o in outcomes] == [True, True, False]
        assert [o["outcome"] for o in outcomes] == [
            "success", "success", "runtime_error",
        ]

        # The older attempt is the one that failed to compile; its message is
        # what the page prints above the table
        older = await query_submission(conn, 2)
        assert older is not None
        assert older["attempt"] == 1
        failures = [
            o for o in older["outcomes"] if o["outcome"] == "compile_failure"
        ]
        assert len(failures) == 1
        assert failures[0]["message"] == "fail"

        # A submission nobody has judged yet still comes back, with an empty
        # outcome array rather than a null the template would have to guard
        queued = await query_submission(conn, 4)
        assert queued is not None
        assert queued["status"] == "queued"
        assert queued["outcomes"] == []
        assert queued["n_tests"] == 0
        assert queued["score"] is None

        assert await query_submission(conn, 999) is None

        # The polled fragment sees the same outcomes, but never the source: it
        # asks every two seconds and a source can be a megabyte
        light = await query_submission_outcomes(conn, 1)
        assert light is not None
        assert "source" not in light
        assert light["outcomes"] == outcomes
        # ...and enough to authorize the viewer, exactly as the page does
        assert [light["user_id"], light["challenge_id"]] == [
            newer["user_id"], newer["challenge_id"]
        ]

        assert await query_submission_outcomes(conn, 999) is None
    await engine.dispose()



########################################################################
# Magic-link authentication. These helpers are plain Core, so unlike the page
# queries they run on the fast in-memory engine - which matters, because they
# are what enforces "a link works once".


def _make_async_engine(db_url="sqlite+aiosqlite:///:memory:"):
    """An async engine over one in-memory database.

    `StaticPool` keeps every connection on the same SQLite database: with the
    default pool a second connection would find an empty schema.
    """
    from sqlalchemy.pool import StaticPool

    return create_async_engine(db_url, poolclass=StaticPool)


async def _async_database():
    engine = _make_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


async def test_get_or_create_user_is_idempotent():
    engine = await _async_database()
    async with engine.connect() as conn:
        created = await get_or_create_user(conn, "dave@unipd.it")
        await conn.commit()
        assert created.nickname == "dave"

        again = await get_or_create_user(conn, "dave@unipd.it")
        assert again.id == created.id

        row = await query_user_by_email(conn, "dave@unipd.it")
        assert row is not None and row.id == created.id
        # An address that was never seen is simply absent.
        assert await query_user_by_email(conn, "nobody@unipd.it") is None
    await engine.dispose()


async def test_course_role_is_the_editors_check():
    """The editor authorizes on the course, not on a challenge: that is what
    lets it answer the question a challenge cannot, "may I create one here?"."""
    engine = await _async_database()
    async with engine.connect() as conn:
        course_id = (
            await conn.execute(
                insert(Course)
                .values(title="Algorithms 101", description="intro")
                .returning(Course.c.id)
            )
        ).scalar_one()
        other_id = (
            await conn.execute(
                insert(Course)
                .values(title="Data Structures", description="trees")
                .returning(Course.c.id)
            )
        ).scalar_one()
        teacher = await insert_user(conn, "alice@unipd.it", "alice", b"", "image/svg+xml")
        student = await insert_user(conn, "bob@unipd.it", "bob", b"", "image/svg+xml")
        await insert_enrollment(conn, teacher, course_id, UserRole.teacher)
        await insert_enrollment(conn, student, course_id, UserRole.student)
        await conn.commit()

        assert await query_course_role(conn, user_id=teacher, course_id=course_id) == UserRole.teacher
        assert await query_course_role(conn, user_id=student, course_id=course_id) == UserRole.student
        # Enrolled on one course says nothing about another
        assert await query_course_role(conn, user_id=teacher, course_id=other_id) is None
    await engine.dispose()


async def test_admin_is_not_just_another_editor():
    """The check behind "may create a course": holding `admin` *somewhere*.

    A teacher is course staff everywhere it matters and still fails it - that
    distinction is the whole point of the flag, and the schema has nowhere else
    to put it, since `role` lives on `enrollment`.
    """
    engine = await _async_database()
    async with engine.connect() as conn:
        course_id = await insert_course(conn, title="Algorithms 101", description="intro")
        admin = await insert_user(conn, "matteo@unipd.it", "matteo", b"", "image/svg+xml")
        teacher = await insert_user(conn, "alice@unipd.it", "alice", b"", "image/svg+xml")
        stranger = await insert_user(conn, "bob@unipd.it", "bob", b"", "image/svg+xml")
        await insert_enrollment(conn, admin, course_id, UserRole.admin)
        await insert_enrollment(conn, teacher, course_id, UserRole.teacher)
        await conn.commit()

        assert await user_is_admin(conn, admin)
        assert not await user_is_admin(conn, teacher)
        # Enrolled nowhere at all
        assert not await user_is_admin(conn, stranger)
    await engine.dispose()


async def test_insert_challenge_and_its_course():
    engine = await _async_database()
    async with engine.connect() as conn:
        course_id = (
            await conn.execute(
                insert(Course)
                .values(title="Algorithms 101", description="intro")
                .returning(Course.c.id)
            )
        ).scalar_one()
        other_id = (
            await conn.execute(
                insert(Course)
                .values(title="Data Structures", description="trees")
                .returning(Course.c.id)
            )
        ).scalar_one()

        challenge_id = await insert_challenge(
            conn,
            course_id=course_id,
            title="Hello",
            description="say hi",
            max_time_s=5.0,
            max_memory_bytes=256 << 20,
            max_threads=1,
            ranking=LeaderboardRanking.total_score,
        )
        await conn.commit()

        row = (
            await conn.execute(select(Challenge).where(Challenge.c.id == challenge_id))
        ).one()
        assert row.title == "Hello"
        assert row.course_id == course_id
        assert row.ranking == LeaderboardRanking.total_score
        # No checker of its own: graded by the bundled equality.py
        assert row.evaluator_path is None

        # The guard behind the editor's URL: the course in the path has to be
        # the challenge's own, or staff of one course could edit another's.
        assert await challenge_in_course(conn, challenge_id, course_id)
        assert not await challenge_in_course(conn, challenge_id, other_id)
    await engine.dispose()


@pytest.mark.integration
async def test_course_page_query_survives_an_empty_course():
    """A course with no challenges is a real course - it is the one whose page
    staff open to create the first one - so it must not come back as NULL."""
    url = _postgres_url()
    sync_engine = create_engine(url)
    metadata.drop_all(sync_engine)
    _make_example_database(url)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        empty_id = (
            await conn.execute(
                insert(Course)
                .values(title="Compilers", description="not started yet")
                .returning(Course.c.id)
            )
        ).scalar_one()
        await conn.commit()

        carol = (await query_user_by_email(conn, "carol@example.com")).id

        populated = await query_course_info(conn, 1, user_id=carol)
        assert populated is not None
        assert [c["title"] for c in populated["challenges"]] == ["Hello", "Sum"]

        empty = await query_course_info(conn, empty_id, user_id=carol)
        assert empty is not None
        assert empty["title"] == "Compilers"
        assert empty["challenges"] == []
    await engine.dispose()


@pytest.mark.integration
async def test_the_course_page_says_who_has_submitted():
    """The badge on a challenge card. `n_mine` is the viewer's own history, so
    it is what a student's "Submitted" means; `n_submitters` is the class, and
    only course staff are shown it."""
    url = _postgres_url()
    sync_engine = create_engine(url)
    metadata.drop_all(sync_engine)
    _make_example_database(url)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@example.com")).id
        erin = (await query_user_by_email(conn, "erin@example.com")).id

        # Carol submitted twice to Hello and once to Sum; dave submitted to
        # Hello as well, so that one has two distinct submitters.
        hers = {
            c["title"]: (c["n_mine"], c["n_submitters"])
            for c in await query_course_challenges(conn, 1, user_id=carol)
        }
        assert hers == {"Hello": (2, 2), "Sum": (1, 1)}

        # Erin is enrolled elsewhere: same challenges, none of them hers, and
        # the class counts are unchanged by who is looking.
        theirs = {
            c["title"]: (c["n_mine"], c["n_submitters"])
            for c in await query_course_challenges(conn, 1, user_id=erin)
        }
        assert theirs == {"Hello": (0, 2), "Sum": (0, 1)}
    await engine.dispose()


@pytest.mark.integration
async def test_challenges_are_listed_in_their_stored_order():
    """`position` is what the arrows write, and both the page query and the
    fragment order by it - with the id breaking a tie, so challenges nobody has
    reordered keep the order they were created in."""
    url = _postgres_url()
    sync_engine = create_engine(url)
    metadata.drop_all(sync_engine)
    _make_example_database(url)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        alice = (await query_user_by_email(conn, "alice@example.com")).id

        # Seeded without touching `position`, so every row sits at the default
        # and the ids alone decide. This is also the case a plain swap cannot
        # handle: exchanging two zeroes moves nothing.
        ordered = await query_ordered_challenge_ids(conn, 1)
        assert [c["title"] for c in await query_course_challenges(conn, 1, user_id=alice)] == [
            "Hello",
            "Sum",
        ]

        await reorder_challenges(conn, list(reversed(ordered)))
        await conn.commit()

        assert await query_ordered_challenge_ids(conn, 1) == list(reversed(ordered))
        assert [c["title"] for c in await query_course_challenges(conn, 1, user_id=alice)] == [
            "Sum",
            "Hello",
        ]
        # The page query nests the same list, and the number it prints on a
        # challenge follows the order rather than the raw column.
        course = await query_course_info(conn, 1, user_id=alice)
        assert [c["title"] for c in course["challenges"]] == ["Sum", "Hello"]
        moved = await query_challenge(conn, user_id=alice, challenge_id=ordered[0])
        assert moved["number"] == 2
    await engine.dispose()


async def test_a_duplicate_address_is_refused():
    """`user.email` is unique: it is the login identifier, and it is the
    backstop for two first logins racing each other."""
    from sqlalchemy.exc import IntegrityError

    engine = await _async_database()
    async with engine.connect() as conn:
        await insert_user(conn, "eve@unipd.it", "eve", b"<svg/>", "image/svg+xml")
        await conn.commit()
        with pytest.raises(IntegrityError):
            await insert_user(conn, "eve@unipd.it", "eve2", b"<svg/>", "image/svg+xml")
    await engine.dispose()
