import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from sqlalchemy import func, insert, or_, select, text, update

import cocco.model as cm
from cocco.avatar import generate_identicon
from cocco.conftest import PUBLISHED, TEST_DATABASE
from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    Submission,
    TestCase,
    TestOutcome,
    User,
)
from cocco.model import LeaderboardRanking, UserRole
from cocco.auth import get_or_create_user
from cocco.repository import (
    QUEUE_CHANNEL,
    block_user,
    challenge_in_course,
    count_running_submissions,
    count_testcase_outcomes,
    course_exists,
    delete_challenge,
    delete_course,
    delete_enrollment,
    insert_administrators,
    insert_challenge,
    insert_course,
    insert_enrollment,
    insert_self_enrollment,
    insert_submission,
    insert_user,
    is_administrator,
    lock_challenge_submissions,
    lock_course_submissions,
    pull_queued_submission,
    query_challenge,
    query_challenge_file_paths,
    query_challenge_leaderboard,
    query_challenge_ranking,
    query_challenge_standings,
    query_challenge_testcases,
    query_challenge_visibility,
    query_course_challenges,
    query_course_info,
    query_course_people,
    query_course_archived_at,
    query_course_role,
    query_course,
    query_courses,
    query_effective_archived_at,
    query_enrollment_key,
    query_ordered_challenge_ids,
    query_sample_testcase,
    query_submission,
    query_submission_outcomes,
    query_user,
    query_user_by_email,
    query_user_profile,
    query_user_role,
    query_user_submissions,
    query_users,
    record_sign_in,
    regrade_challenge,
    reorder_challenges,
    set_challenge_archived,
    set_course_archived,
    set_enrollment_role,
    unblock_user,
)
from cocco import schema
from cocco.schema import SubmissionRunData


async def _course_id(conn, title):
    """The seeded course with this title. `_seed` builds them
    from a list, so the ids are not known to the test that wants one."""
    return (
        await conn.execute(select(Course.c.id).where(Course.c.title == title))
    ).scalar_one()


def _seed(engine):
    """Fill an empty database with the cast every query in this file is asked
    about: six users, three courses, five challenges, three test cases each and
    nine submissions."""
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        users = [
            ("root@unipd.it", "root"),
            ("alice@unipd.it", "alice"),
            ("bob@unipd.it", "bob"),
            ("carol@unipd.it", "carol"),
            ("dave@unipd.it", "dave"),
            ("erin@unipd.it", "erin"),
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
                    # Published. NULL here means hidden, and the page queries
                    # drop a hidden challenge for everybody but staff - so a
                    # seed that left it out would test the filter rather than
                    # whatever the test was about.
                    visible_from=PUBLISHED,
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


async def test_get_queued_submission_returns_oldest_queued(db):
    engine = db
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@unipd.it",
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


async def test_queued_submission_carries_the_challenge_evaluator(db):
    """The judge learns which checker to grade with from the run data, and
    from nowhere else."""
    engine = db

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@unipd.it",
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


async def test_queued_submission_skips_retired_testcases(db):
    """Retiring a test case is what stops it grading new submissions."""
    engine = db

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@unipd.it",
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
                enqueued=datetime.now(timezone.utc),
            )
        )

    with engine.connect() as conn:
        result = await pull_queued_submission(conn)

    assert result is not None
    assert [t.execution_order for t in result.test_cases] == [0, 2]


async def test_get_queued_submission_returns_none_when_empty(db):
    engine = db
    with engine.connect() as conn:
        result = await pull_queued_submission(conn)
    assert result is None


def _user_and_challenge(engine) -> tuple[int, int]:
    """One user and one challenge, ids returned, for a queue to be about."""
    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@unipd.it",
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
    return user_id, challenge_id


def _queue_submissions(engine, n: int) -> list[int]:
    """`n` queued submissions to one challenge, oldest first."""
    user_id, challenge_id = _user_and_challenge(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        return [
            conn.execute(
                insert(Submission)
                .values(
                    challenge_id=challenge_id,
                    user_id=user_id,
                    language="python",
                    filename="solution.py",
                    source=f"print({i})",
                    status=cm.SubmissionStatus.queued,
                    enqueued=now + timedelta(seconds=i),
                )
                .returning(Submission.c.id)
            ).scalar_one()
            for i in range(n)
        ]


async def test_a_claim_passes_over_a_row_another_claimer_holds(db):
    """`SKIP LOCKED`: while one claimer has the oldest row locked, the next
    takes the second oldest instead of waiting - and instead of claiming the
    same row again once the first commits, which is what it used to do. The
    lock timeout turns the old behaviour into a failure rather than a hang."""
    engine = db
    oldest, second = _queue_submissions(engine, 2)

    with engine.connect() as holder:
        holder.execute(
            select(Submission.c.id).where(Submission.c.id == oldest).with_for_update()
        )
        with engine.connect() as conn:
            conn.execute(text("SET lock_timeout = '2s'"))
            result = await pull_queued_submission(conn)
        holder.rollback()

    assert result is not None
    assert result.submission_id == second


def test_concurrent_claimers_never_get_the_same_row(db):
    """Eight claimers on eight connections, draining forty submissions at once:
    every submission is claimed, and by exactly one of them."""
    engine = db
    queued = _queue_submissions(engine, 40)

    def drain() -> list[int]:
        claimed = []
        with engine.connect() as conn:
            while (run_data := asyncio.run(pull_queued_submission(conn))) is not None:
                claimed.append(run_data.submission_id)
        return claimed

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [i for ids in pool.map(lambda _: drain(), range(8)) for i in ids]

    assert sorted(claims) == queued


def _challenge_ids(engine) -> list[int]:
    with engine.connect() as conn:
        return list(conn.execute(select(Challenge.c.id)).scalars())


def _listener():
    """A connection listening on the judge's channel, the way the judge does."""
    conn = psycopg.connect(f"postgresql:///{TEST_DATABASE}", autocommit=True)
    conn.execute(f"LISTEN {QUEUE_CHANNEL}")
    return conn


async def test_a_submission_wakes_the_judge_when_it_commits(db, app_engine):
    """The notification goes out with the commit and not before, so a judge
    woken by it always finds the row it was woken for."""
    user_id, challenge_id = _user_and_challenge(db)
    with _listener() as listener:
        async with app_engine.connect() as conn:
            await insert_submission(
                conn,
                user_id=user_id,
                challenge_id=challenge_id,
                language="python",
                filename="a.py",
                source="",
            )
            assert list(listener.notifies(timeout=0.2)) == []
            await conn.commit()
        [notification] = listener.notifies(timeout=2, stop_after=1)
        assert notification.channel == QUEUE_CHANNEL


async def test_a_regrade_wakes_the_judge_unless_it_requeued_nothing(db, app_engine):
    _queue_submissions(db, 1)
    [challenge_id] = _challenge_ids(db)
    with _listener() as listener:
        async with app_engine.connect() as conn:
            assert await regrade_challenge(conn, challenge_id=challenge_id + 1) == 0
            await conn.commit()
        assert list(listener.notifies(timeout=0.2)) == []

        async with app_engine.connect() as conn:
            assert await regrade_challenge(conn, challenge_id=challenge_id) == 1
            await conn.commit()
        assert len(list(listener.notifies(timeout=2, stop_after=1))) == 1


# The json_agg / row_to_json queries backing the web pages. They return one
# nested document rather than a row set, so what is asserted below is the shape
# a template walks - see `DESIGN.md` for why the pages are built this way.
async def test_challenge_page_queries(example_database, app_engine):
    engine = app_engine
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

        # A regrade is also what puts the judging lifecycle back: stamp the
        # rows first, so that them being NULL afterwards means something.
        await conn.execute(
            update(Submission)
            .where(Submission.c.challenge_id == 1)
            .values(started_at=func.now(), finished_at=func.now())
        )

        # Rewrites the fixture, so it goes last. The connection is never
        # committed: like the other mutations, this one leaves that to the
        # caller, and the assertions below read it back inside the transaction.
        assert await regrade_challenge(conn, 1) == 3

        # Both timestamps went with the status. Leaving them would report a
        # queued submission as one that had already finished.
        assert (
            await conn.execute(
                select(func.count())
                .select_from(Submission)
                .where(
                    Submission.c.challenge_id == 1,
                    or_(
                        Submission.c.started_at.is_not(None),
                        Submission.c.finished_at.is_not(None),
                    ),
                )
            )
        ).scalar_one() == 0

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


async def test_submission_page_queries(example_database, app_engine):
    """The two queries behind the single-submission page.

    Submission ids follow the order of the fixture's `submissions` list, so 1
    and 2 are carol's two attempts at "Hello" - the older one (2) being the one
    the fixture's outcome cycle gives a compile failure.
    """
    engine = app_engine
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



########################################################################
# Accounts, sign-in and the two site-wide powers: who exists, who administers
# the site, and who is blocked. Plain Core rather than the raw SQL above, and
# read back through the same helpers the routes use.


@pytest.fixture
def example_database(db):
    """`_seed` on the emptied database, as a fixture rather than the four-line
    preamble that stood at the top of twenty tests.

    It hands back the sync engine and not a map of ids, because there never was
    one: a test looks a course up by title with `_course_id`, or names the id
    the seed order fixes - carol is user 4, "Hello" is challenge 1 - and what
    keeps that true from one test to the next is the `RESTART IDENTITY` in
    conftest's truncation.
    """
    _seed(db)
    return db


async def test_get_or_create_user_is_idempotent(app_engine):
    engine = app_engine
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


async def test_record_sign_in_stamps_the_first_login_once(app_engine):
    """`first_login_at` is written by the first sign-in and by no later one.

    It is the column that separates "has never been near the login page" from
    "was created by a teacher writing out a roster", which `created_at` cannot
    answer - `get_or_create_user` is the same function on both paths. Each call
    commits, because `now()` is the *transaction's* clock: two stamps inside one
    transaction are the same instant by design.
    """
    engine = app_engine

    async def logins(conn, user_id):
        # Read straight off the row: `_USER_COLUMNS` deliberately does not carry
        # these, and nothing should make it - it is the list `schema.User` is
        # built from, and the reason it is spelled once is `blocked_at`.
        return (
            await conn.execute(
                select(User.c.first_login_at, User.c.last_login_at).where(
                    User.c.id == user_id
                )
            )
        ).one()

    async with engine.connect() as conn:
        user = await get_or_create_user(conn, "dave@unipd.it")
        await conn.commit()

        # Created, never signed in - which is exactly the pre-enrolled roster.
        before = await logins(conn, user.id)
        assert before.first_login_at is None and before.last_login_at is None

        await record_sign_in(conn, user.id)
        await conn.commit()
        first = await logins(conn, user.id)
        assert first.first_login_at is not None
        assert first.last_login_at is not None

        await record_sign_in(conn, user.id)
        await conn.commit()
        second = await logins(conn, user.id)
        assert second.first_login_at == first.first_login_at
        assert second.last_login_at >= first.last_login_at


async def test_course_role_is_the_editors_check(app_engine):
    """The editor authorizes on the course, not on a challenge: that is what
    lets it answer the question a challenge cannot, "may I create one here?"."""
    engine = app_engine
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


async def test_an_administrator_is_not_a_role_on_a_course(app_engine):
    """The check behind "may create a course": a row in `administrator`.

    A teacher is course staff on the course they teach and still fails it -
    that distinction is the whole point, and it is why the answer does not come
    from `enrollment` at all.
    """
    engine = app_engine
    async with engine.connect() as conn:
        course_id = await insert_course(conn, title="Algorithms 101", description="intro")
        teacher = await insert_user(conn, "alice@unipd.it", "alice", b"", "image/svg+xml")
        await insert_enrollment(conn, teacher, course_id, UserRole.teacher)
        await insert_administrators(conn, ["matteo@unipd.it"])
        await conn.commit()

        assert await is_administrator(conn, "matteo@unipd.it")
        assert not await is_administrator(conn, "alice@unipd.it")
        # An address with no account behind it at all is still not one
        assert not await is_administrator(conn, "bob@unipd.it")


async def test_an_administrator_needs_no_account_yet(app_engine):
    """The table is keyed on the address precisely so that `cocco.toml` can name
    someone who has never signed in - the account only appears on the first
    accepted callback from Google."""
    engine = app_engine
    async with engine.connect() as conn:
        await insert_administrators(conn, ["nobody@unipd.it"])
        await conn.commit()
        assert await is_administrator(conn, "nobody@unipd.it")


async def test_bootstrapping_administrators_is_idempotent(app_engine):
    """The configured list is replayed on every boot, so inserting it twice has
    to be a no-op rather than a primary key violation."""
    engine = app_engine
    async with engine.connect() as conn:
        await insert_administrators(conn, ["matteo@unipd.it", "alice@unipd.it"])
        await insert_administrators(conn, ["matteo@unipd.it", "bob@unipd.it"])
        # An empty list is a no-op too, not an empty INSERT
        await insert_administrators(conn, [])
        await conn.commit()

        rows = (await conn.execute(select(Administrator.c.email))).scalars().all()
        assert sorted(rows) == ["alice@unipd.it", "bob@unipd.it", "matteo@unipd.it"]


async def test_blocking_never_touches_an_administrator(app_engine):
    """The rule lives in `block_user`'s WHERE clause, not in a check before it.

    Appointing is one-way through the app, so a blocked administrator would be
    a lockout with no way back - and a check and a write that are two
    statements can be told apart by an appointment landing between them.
    """
    engine = app_engine
    async with engine.connect() as conn:
        admin = await insert_user(conn, "matteo@unipd.it", "matteo", b"", "image/svg+xml")
        student = await insert_user(conn, "bob@unipd.it", "bob", b"", "image/svg+xml")
        await insert_administrators(conn, ["matteo@unipd.it"])
        await conn.commit()

        assert not await block_user(conn, admin, "abusive")
        assert (await query_user(conn, admin)).blocked_at is None

        assert await block_user(conn, student, "spamming the judge")
        await conn.commit()
        row = await query_user(conn, student)
        assert row.blocked_at is not None
        assert row.blocked_reason == "spamming the judge"


async def test_blocking_an_absent_user_changes_nothing(app_engine):
    """False for an id that is not there, exactly as for an administrator: the
    route says the same thing about both, because in both cases nothing
    happened."""
    engine = app_engine
    async with engine.connect() as conn:
        assert not await block_user(conn, 404, "who?")
        assert not await unblock_user(conn, 404)


async def test_unblocking_forgets_the_reason(app_engine):
    """The reason exists to be shown to the person it shut out, so it has no
    life after they are let back in."""
    engine = app_engine
    async with engine.connect() as conn:
        student = await insert_user(conn, "bob@unipd.it", "bob", b"", "image/svg+xml")
        await conn.commit()

        assert await block_user(conn, student, "spamming the judge")
        assert await unblock_user(conn, student)
        await conn.commit()

        row = await query_user(conn, student)
        assert row.blocked_at is None
        assert row.blocked_reason is None


async def test_a_block_reaches_both_user_lookups(app_engine):
    """`get_user` reads nothing but these two, so a trimmed column list in
    either one would fail open - the block would simply not be there to see."""
    engine = app_engine
    async with engine.connect() as conn:
        student = await insert_user(conn, "bob@unipd.it", "bob", b"", "image/svg+xml")
        await block_user(conn, student, "spamming the judge")
        await conn.commit()

        by_id = await query_user(conn, student)
        by_email = await query_user_by_email(conn, "bob@unipd.it")
        for row in (by_id, by_email):
            assert row is not None
            assert row.blocked_at is not None
            assert row.blocked_reason == "spamming the judge"

        # And the model built from either of them carries it, which is what
        # `get_user` actually refuses on.
        assert schema.User.model_validate(by_id).blocked_at is not None


async def test_insert_challenge_and_its_course(app_engine):
    engine = app_engine
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


async def test_course_page_query_survives_an_empty_course(example_database, app_engine):
    """A course with no challenges is a real course - it is the one whose page
    staff open to create the first one - so it must not come back as NULL."""
    engine = app_engine
    async with engine.connect() as conn:
        empty_id = (
            await conn.execute(
                insert(Course)
                .values(title="Compilers", description="not started yet")
                .returning(Course.c.id)
            )
        ).scalar_one()
        await conn.commit()

        carol = (await query_user_by_email(conn, "carol@unipd.it")).id

        populated = await query_course_info(conn, 1, user_id=carol)
        assert populated is not None
        assert [c["title"] for c in populated["challenges"]] == ["Hello", "Sum"]

        empty = await query_course_info(conn, empty_id, user_id=carol)
        assert empty is not None
        assert empty["title"] == "Compilers"
        assert empty["challenges"] == []


async def test_the_course_page_says_who_has_submitted(example_database, app_engine):
    """The badge on a challenge card. `n_mine` is the viewer's own history, so
    it is what a student's "Submitted" means; `n_submitters` is the class, and
    only course staff are shown it."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        erin = (await query_user_by_email(conn, "erin@unipd.it")).id

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


async def test_challenges_are_listed_in_their_stored_order(example_database, app_engine):
    """`position` is what the arrows write, and both the page query and the
    fragment order by it - with the id breaking a tie, so challenges nobody has
    reordered keep the order they were created in."""
    engine = app_engine
    async with engine.connect() as conn:
        alice = (await query_user_by_email(conn, "alice@unipd.it")).id

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


async def test_courses_page_query(example_database, app_engine):
    """The home page's list: every course, split by whether the viewer is on
    it, and filtered by the page's search box."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id

        # carol is a student on two of the three courses. Both halves come back
        # sorted by title, and the enrolled rows carry the role the card prints.
        courses = await query_courses(conn, user_id=carol)
        assert [c["title"] for c in courses["enrolled"]] == [
            "Algorithms 101",
            "Data Structures",
        ]
        assert {c["role"] for c in courses["enrolled"]} == {"student"}
        assert [c["title"] for c in courses["non_enrolled"]] == ["Systems Programming"]

        # Somebody with no enrollments at all gets an empty list, not a null:
        # that is what the `coalesce` in the query is for.
        root = (await query_user_by_email(conn, "root@unipd.it")).id
        as_root = await query_courses(conn, user_id=root)
        assert as_root["enrolled"] == []
        assert len(as_root["non_enrolled"]) == 3

        # The filter is a case-insensitive substring of the title, and it
        # applies to both halves at once.
        filtered = await query_courses(conn, user_id=carol, q="dATa")
        assert [c["title"] for c in filtered["enrolled"]] == ["Data Structures"]
        assert filtered["non_enrolled"] == []

        crossing = await query_courses(conn, user_id=carol, q="st")
        assert [c["title"] for c in crossing["enrolled"]] == ["Data Structures"]
        assert [c["title"] for c in crossing["non_enrolled"]] == ["Systems Programming"]

        # The title only - "trees, graphs, hashing" is a description
        assert await query_courses(conn, user_id=carol, q="hashing") == {
            "enrolled": [],
            "non_enrolled": [],
        }

        # A substring test, not a pattern: the characters LIKE would read as
        # wildcards match themselves and nothing else.
        for pattern in ("100%", "_", "%"):
            wild = await query_courses(conn, user_id=carol, q=pattern)
            assert wild["enrolled"] == []
            assert wild["non_enrolled"] == []

        # An empty filter is no filter, i.e. the unfiltered call above
        assert await query_courses(conn, user_id=carol, q="") == courses


async def test_users_page_query(example_database, app_engine):
    """The admin page's list: everyone with an account, plus the appointed
    addresses that have none yet."""
    engine = app_engine
    async with engine.connect() as conn:
        await insert_administrators(conn, ["alice@unipd.it", "nobody@unipd.it"])
        bob = (await query_user_by_email(conn, "bob@unipd.it")).id
        await block_user(conn, bob, "spamming the judge")
        await conn.commit()

        users = await query_users(conn)
        by_nickname = {u["nickname"]: u for u in users["accounts"]}

        # Sorted by nickname, and everyone with an account is here - including
        # root, who is on no course and has submitted nothing.
        assert [u["nickname"] for u in users["accounts"]] == [
            "alice", "bob", "carol", "dave", "erin", "root",
        ]

        # The badge comes from `administrator`, which is not a role on a course:
        # alice teaches two courses and bob teaches one, and only alice is here.
        assert by_nickname["alice"]["is_admin"]
        assert not by_nickname["bob"]["is_admin"]

        assert by_nickname["bob"]["is_blocked"]
        assert by_nickname["bob"]["blocked_reason"] == "spamming the judge"
        assert by_nickname["bob"]["blocked_at"] is not None
        assert not by_nickname["carol"]["is_blocked"]
        assert by_nickname["carol"]["blocked_reason"] is None

        # The two counts, against the seeded enrollments and submissions.
        assert (by_nickname["carol"]["n_courses"], by_nickname["carol"]["n_submissions"]) == (2, 4)
        assert (by_nickname["dave"]["n_courses"], by_nickname["dave"]["n_submissions"]) == (2, 3)
        # Zero and not null: a left join over somebody who has done neither.
        assert (by_nickname["root"]["n_courses"], by_nickname["root"]["n_submissions"]) == (0, 0)
        assert by_nickname["root"]["last_submitted"] is None
        assert by_nickname["carol"]["last_submitted"] is not None

        # Appointed and never signed in: the whole reason this list exists,
        # since `administrator` is keyed on an address and `user` on an account.
        assert [p["email"] for p in users["pending_administrators"]] == ["nobody@unipd.it"]

        # The filter matches the nickname...
        assert [u["nickname"] for u in (await query_users(conn, q="CAro"))["accounts"]] == ["carol"]
        # ...and the address, which is the half that matters when somebody is
        # looking up a person they only know by their institutional address.
        found = await query_users(conn, q="dave@unipd")
        assert [u["nickname"] for u in found["accounts"]] == ["dave"]
        # It reaches the appointed addresses too, so a filtered page does not
        # quietly drop half of what it is for.
        pending = await query_users(conn, q="nobody")
        assert pending["accounts"] == []
        assert [p["email"] for p in pending["pending_administrators"]] == ["nobody@unipd.it"]

        # A substring test, not a pattern: the characters LIKE would read as
        # wildcards match themselves and nothing else.
        for pattern in ("100%", "_", "%"):
            wild = await query_users(conn, q=pattern)
            assert wild["accounts"] == []
            assert wild["pending_administrators"] == []

        # An empty filter is no filter, i.e. the unfiltered call above
        assert await query_users(conn, q="") == users


async def test_user_profile_query(example_database, app_engine):
    """One person's page: who they are, and every course they are on."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        profile = await query_user_profile(conn, carol)

        assert (profile["email"], profile["nickname"]) == ("carol@unipd.it", "carol")
        assert not profile["is_admin"]
        assert not profile["is_blocked"]
        assert profile["blocked_at"] is None
        # The avatar has a route of its own and never rides along in the JSON.
        assert "image" not in profile

        # Site-wide, and beside the per-course counts rather than their sum.
        assert profile["n_submissions"] == 4
        assert profile["last_submitted"] is not None

        # Staff first, then by title - and the counts are per course, which is
        # the number a profile is actually asked for.
        assert [(c["title"], c["role"], c["n_submissions"]) for c in profile["courses"]] == [
            ("Algorithms 101", "student", 3),
            ("Data Structures", "student", 1),
        ]

        # Somebody on nothing at all gets an empty list, not a null to iterate.
        root = (await query_user_by_email(conn, "root@unipd.it")).id
        empty = await query_user_profile(conn, root)
        assert empty["courses"] == []
        assert (empty["n_submissions"], empty["last_submitted"]) == (0, None)

        # `administrator` is keyed on the address, and this is the one place a
        # profile asks about it.
        await insert_administrators(conn, ["alice@unipd.it"])
        bob = (await query_user_by_email(conn, "bob@unipd.it")).id
        await block_user(conn, bob, "spamming the judge")
        await conn.commit()

        alice = (await query_user_by_email(conn, "alice@unipd.it")).id
        staff = await query_user_profile(conn, alice)
        assert staff["is_admin"]
        # She teaches, so her courses carry the role that says so.
        assert {c["role"] for c in staff["courses"]} == {"teacher"}

        blocked = await query_user_profile(conn, bob)
        assert blocked["is_blocked"]
        assert blocked["blocked_reason"] == "spamming the judge"

        # No such account is no document at all, which the route turns into a
        # 404 rather than rendering a page about nobody.
        assert await query_user_profile(conn, 99999) is None


async def test_self_enrollment(example_database, app_engine):
    """The home page's `Enroll!` button: it makes students, only students, and
    never unmakes anything. It rests on `ON CONFLICT DO NOTHING`, which is what
    makes a double click a no-op instead of an IntegrityError."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        systems = (await query_courses(conn, user_id=carol))["non_enrolled"][0]
        assert systems["title"] == "Systems Programming"

        assert await course_exists(conn, systems["id"])
        # A POST naming a course that is not there is a 404, not a foreign key
        # violation on the way in
        assert not await course_exists(conn, 10_000)

        await insert_self_enrollment(conn, user_id=carol, course_id=systems["id"])
        await conn.commit()
        assert (
            await query_course_role(conn, user_id=carol, course_id=systems["id"])
        ) == UserRole.student

        # ...and the course has crossed over to the half the button moved it to
        after = await query_courses(conn, user_id=carol)
        assert [c["title"] for c in after["enrolled"]] == [
            "Algorithms 101",
            "Data Structures",
            "Systems Programming",
        ]
        assert after["non_enrolled"] == []

        # A double click is a silent no-op rather than an IntegrityError
        await insert_self_enrollment(conn, user_id=carol, course_id=systems["id"])
        await conn.commit()
        assert await query_courses(conn, user_id=carol) == after

        # The guarantee that matters: an existing role is never overwritten, so
        # this can neither promote anyone nor demote the course's own teacher.
        bob = (await query_user_by_email(conn, "bob@unipd.it")).id
        await insert_self_enrollment(conn, user_id=bob, course_id=systems["id"])
        await conn.commit()
        assert (
            await query_course_role(conn, user_id=bob, course_id=systems["id"])
        ) == UserRole.teacher


async def test_the_roster_is_the_enrolled_and_counts_this_course_only(example_database, app_engine):
    """`query_course_people` is the class list, not the scoreboard's cohort:
    strictly the enrolled, and the submission counts stop at this course."""
    engine = app_engine
    async with engine.connect() as conn:
        algorithms = await _course_id(conn, "Algorithms 101")
        roster = await query_course_people(conn, algorithms)

        # Staff first, then by name - the people who run the course on top.
        assert [p["nickname"] for p in roster["people"]] == ["alice", "carol", "dave"]
        assert roster["n_teachers"] == 1
        by_name = {p["nickname"]: p for p in roster["people"]}
        assert by_name["alice"]["role"] == "teacher"
        assert by_name["carol"]["role"] == "student"
        # Bob teaches Systems Programming and is nothing here.
        assert "bob" not in by_name

        # The counts are over this course, not over everything they have ever
        # submitted - a teacher looking at their class wants to know who has
        # started here.
        everywhere = {
            u["nickname"]: u["n_submissions"]
            for u in (await query_users(conn))["accounts"]
        }
        for nickname, person in by_name.items():
            assert person["n_submissions"] <= everywhere[nickname]

        # The filter is a substring test on either field, and empty is no filter
        assert [
            p["nickname"] for p in (await query_course_people(conn, algorithms, q="car"))["people"]
        ] == ["carol"]
        assert await query_course_people(conn, algorithms, q="") == roster

        # Counted outside the filter: hiding the other teachers must not make
        # the visible one look like the last.
        assert (await query_course_people(conn, algorithms, q="dave"))["n_teachers"] == 1


async def test_a_course_never_loses_its_last_teacher(example_database, app_engine):
    """The guard both writes carry, from underneath the routes: it lives in the
    WHERE clause, so the rule and the write are one statement."""
    engine = app_engine
    async with engine.connect() as conn:
        algorithms = await _course_id(conn, "Algorithms 101")
        alice = (await query_user_by_email(conn, "alice@unipd.it")).id
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id

        # Alice is the only teacher, so neither way of taking that away works
        assert not await delete_enrollment(conn, algorithms, alice)
        assert not await set_enrollment_role(
            conn, algorithms, alice, UserRole.student
        )
        await conn.commit()
        assert (
            await query_course_role(conn, user_id=alice, course_id=algorithms)
        ) == UserRole.teacher

        # Promoting *to* teacher can never trip the guard
        assert await set_enrollment_role(conn, algorithms, carol, UserRole.teacher)
        await conn.commit()
        # ...and with a second one there is nothing left to protect
        assert await delete_enrollment(conn, algorithms, alice)
        await conn.commit()
        assert (
            await query_course_role(conn, user_id=alice, course_id=algorithms)
        ) is None

        # A row that is not there is False too, and the route says the same
        # thing about both, because in both cases nothing happened.
        assert not await delete_enrollment(conn, algorithms, alice)


async def test_setting_a_role_is_one_upsert(example_database, app_engine):
    """"Enrol them as a teacher" and "make them a teacher" name one row:
    `enrollment` is keyed on (user_id, course_id)."""
    engine = app_engine
    async with engine.connect() as conn:
        systems = await _course_id(conn, "Systems Programming")
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id

        # Not on the course at all: an insert
        assert (
            await query_course_role(conn, user_id=carol, course_id=systems)
        ) is None
        assert await set_enrollment_role(conn, systems, carol, UserRole.collaborator)
        await conn.commit()
        assert (
            await query_course_role(conn, user_id=carol, course_id=systems)
        ) == UserRole.collaborator

        # On it already: an update, not an IntegrityError
        assert await set_enrollment_role(conn, systems, carol, UserRole.student)
        await conn.commit()
        assert (
            await query_course_role(conn, user_id=carol, course_id=systems)
        ) == UserRole.student


async def test_the_enrollment_key_is_reported_but_never_returned(example_database, app_engine):
    """Both course queries feed pages served to people who do not know the key,
    so they answer whether one is wanted and never what it is."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        # carol is a student on Algorithms 101 and not on Systems Programming,
        # so one of each half of the home page gets a key
        await conn.execute(
            update(Course)
            .where(Course.c.title.in_(["Algorithms 101", "Systems Programming"]))
            .values(enrollment_key="Sesame-2026")
        )
        await conn.commit()

        courses = await query_courses(conn, user_id=carol)
        by_title = {
            c["title"]: c for c in courses["enrolled"] + courses["non_enrolled"]
        }
        assert by_title["Algorithms 101"]["requires_key"] is True
        assert by_title["Systems Programming"]["requires_key"] is True
        assert by_title["Data Structures"]["requires_key"] is False
        # Not "absent from the rendered page" - absent from the document
        assert all("enrollment_key" not in c for c in by_title.values())
        assert "Sesame-2026" not in json.dumps(courses)

        systems_id = by_title["Systems Programming"]["id"]
        preview = await query_course(conn, systems_id)
        assert preview.requires_key is True
        assert "enrollment_key" not in preview._mapping

        # ...and the one query that does read it, which exists to compare it
        assert await query_enrollment_key(conn, systems_id) == "Sesame-2026"
        data_structures = by_title["Data Structures"]["id"]
        assert await query_enrollment_key(conn, data_structures) is None


async def test_a_duplicate_address_is_refused(app_engine):
    """`user.email` is unique: it is the login identifier, and it is the
    backstop for two first logins racing each other."""
    from sqlalchemy.exc import IntegrityError

    engine = app_engine
    async with engine.connect() as conn:
        await insert_user(conn, "eve@unipd.it", "eve", b"<svg/>", "image/svg+xml")
        await conn.commit()
        with pytest.raises(IntegrityError):
            await insert_user(conn, "eve@unipd.it", "eve2", b"<svg/>", "image/svg+xml")


########################################################################
# Archiving and deleting. No foreign key in `model.py` declares an `ON DELETE`,
# so `delete_challenge` spells the cascade out by hand and the database's job is
# to raise if the order is ever got wrong - which is what makes these tests
# worth anything, and why they have to run against a database that enforces
# them.


async def _challenge_id(conn, title):
    return (
        await conn.execute(select(Challenge.c.id).where(Challenge.c.title == title))
    ).scalar_one()


async def test_an_archived_course_is_listed_only_to_the_people_on_it(example_database, app_engine):
    """The offer is withdrawn, not the course: it drops out of the half nobody
    is enrolled in and stays in the half they are."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        systems = await _course_id(conn, "Systems Programming")
        algorithms = await _course_id(conn, "Algorithms 101")

        await set_course_archived(conn, systems, True)
        await set_course_archived(conn, algorithms, True)
        await conn.commit()

        courses = await query_courses(conn, user_id=carol)
        # Carol is on Algorithms 101, so it stays - carrying the timestamp the
        # card badges itself with.
        enrolled = {c["title"]: c for c in courses["enrolled"]}
        assert enrolled["Algorithms 101"]["archived_at"] is not None
        assert enrolled["Data Structures"]["archived_at"] is None
        # She is not on Systems Programming, and it cannot be joined, so it is
        # not offered at all.
        assert courses["non_enrolled"] == []

        # An administrator asks for all of them: they are the only person who
        # can unarchive one, so they have to be able to find it.
        as_admin = await query_courses(conn, user_id=carol, include_archived=True)
        assert [c["title"] for c in as_admin["non_enrolled"]] == [
            "Systems Programming"
        ]

        # And unarchiving clears the column rather than writing a second one.
        await set_course_archived(conn, systems, False)
        await conn.commit()
        assert await query_course_archived_at(conn, systems) is None
        reopened = await query_courses(conn, user_id=carol)
        assert [c["title"] for c in reopened["non_enrolled"]] == [
            "Systems Programming"
        ]


async def test_a_challenge_under_an_archived_course_is_archived_in_effect(example_database, app_engine):
    """A challenge is archived when it says so or when its course does, and
    every query that reports it - the single-row read, the challenge page and
    the course's card list - says the same, while the challenge's own column
    stays its own."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        algorithms = await _course_id(conn, "Algorithms 101")
        hello = await _challenge_id(conn, "Hello")
        summing = await _challenge_id(conn, "Sum")

        # Nothing archived: every reading of it is None.
        assert await query_effective_archived_at(conn, hello) is None
        page = await query_challenge(conn, user_id=carol, challenge_id=hello)
        assert page["effective_archived_at"] is None

        # One challenge archived on its own.
        await set_challenge_archived(conn, summing, True)
        await conn.commit()
        assert await query_effective_archived_at(conn, summing) is not None
        assert await query_effective_archived_at(conn, hello) is None

        # Now the course. `Hello`'s own column is still NULL, and every query
        # has to report it as archived anyway.
        await set_course_archived(conn, algorithms, True)
        await conn.commit()

        own = (
            await conn.execute(
                select(Challenge.c.archived_at).where(Challenge.c.id == hello)
            )
        ).scalar_one()
        assert own is None

        assert await query_effective_archived_at(conn, hello) is not None
        page = await query_challenge(conn, user_id=carol, challenge_id=hello)
        assert page["effective_archived_at"] is not None
        # The card's list answers the same way...
        cards = await query_course_challenges(conn, algorithms, user_id=carol)
        assert all(c["effective_archived_at"] is not None for c in cards)
        # ...and the challenge's own column rides along beside it, untouched,
        # which is what the challenge editor's danger zone reads.
        by_title = {c["title"]: c for c in cards}
        assert by_title["Hello"]["archived_at"] is None
        assert by_title["Sum"]["archived_at"] is not None


async def test_visible_from_decides_whether_a_challenge_is_visible(example_database, app_engine):
    """NULL is hidden, a future instant is not visible yet and a past one is,
    and the single-row read, the challenge page and the course's card list all
    say so. `test_a_challenge_under_an_archived_course_is_archived_in_effect`'s
    counterpart for the other end of a challenge's life.
    """
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        algorithms = await _course_id(conn, "Algorithms 101")
        hello = await _challenge_id(conn, "Hello")

        # Published by the seed, and every reading of it says so.
        assert (await query_challenge_visibility(conn, hello))[1] is True
        page = await query_challenge(conn, user_id=carol, challenge_id=hello)
        assert page["is_visible"] is True

        for visible_from, expected in (
            (None, False),
            (datetime.now(timezone.utc) + timedelta(days=7), False),
            (datetime.now(timezone.utc) - timedelta(days=7), True),
        ):
            await conn.execute(
                update(Challenge)
                .where(Challenge.c.id == hello)
                .values(visible_from=visible_from)
            )
            await conn.commit()

            stored, visible = await query_challenge_visibility(conn, hello)
            assert visible is expected
            assert stored == visible_from
            page = await query_challenge(conn, user_id=carol, challenge_id=hello)
            assert page["is_visible"] is expected
            # ...and so does the card's list.
            cards = await query_course_challenges(
                conn, algorithms, user_id=carol, include_hidden=True
            )
            by_title = {c["title"]: c for c in cards}
            assert by_title["Hello"]["is_visible"] is expected

        # A challenge that does not exist answers as a hidden one does, which is
        # the right answer to both halves: the caller 404s.
        assert await query_challenge_visibility(conn, 10_000) == (None, False)


async def test_a_hidden_challenge_is_dropped_and_keeps_its_number(example_database, app_engine):
    """Two things at once, because they are one bug. Hidden challenges leave a
    student's list, scheduled ones stay in it as a teaser - and the number a
    card prints comes off the row, ranked over the whole course, so a student is
    not told to solve a "Challenge 02" that staff call 03."""
    engine = app_engine
    async with engine.connect() as conn:
        carol = (await query_user_by_email(conn, "carol@unipd.it")).id
        algorithms = await _course_id(conn, "Algorithms 101")
        hello = await _challenge_id(conn, "Hello")
        summing = await _challenge_id(conn, "Sum")

        # Hidden, and the *first* of the two, which is what makes the numbering
        # half of this test bite: counting rows on the page would renumber
        # everything after it.
        await conn.execute(
            update(Challenge)
            .where(Challenge.c.id == hello)
            .values(visible_from=None, position=0)
        )
        await conn.execute(
            update(Challenge)
            .where(Challenge.c.id == summing)
            .values(visible_from=datetime.now(timezone.utc) + timedelta(days=7),
                    position=1)
        )
        await conn.commit()

        staff = await query_course_challenges(
            conn, algorithms, user_id=carol, include_hidden=True
        )
        assert [c["title"] for c in staff] == ["Hello", "Sum"]
        assert [c["number"] for c in staff] == [1, 2]

        # The student loses the hidden one and keeps the scheduled one, which is
        # the teaser - with the date it opens on, so the card can print it.
        student = await query_course_challenges(conn, algorithms, user_id=carol)
        assert [c["title"] for c in student] == ["Sum"]
        assert student[0]["is_visible"] is False
        assert student[0]["visible_from"] is not None
        # ...and it is still number 2 to them, exactly as it is to staff.
        assert student[0]["number"] == 2

        # The nested document the course page actually renders says the same.
        page = await query_course_info(conn, algorithms, user_id=carol)
        assert [c["title"] for c in page["challenges"]] == ["Sum"]
        assert page["challenges"][0]["number"] == 2

        # The default is the safe answer: a caller who forgets the flag shows a
        # student's list, never a teacher's.
        assert len(await query_course_challenges(conn, algorithms, user_id=carol)) == 1


async def test_deleting_a_challenge_leaves_nothing_behind(example_database, app_engine):
    """The hand-ordered cascade, against a database that enforces every
    foreign key it walks past."""
    engine = app_engine
    async with engine.connect() as conn:
        hello = await _challenge_id(conn, "Hello")
        summing = await _challenge_id(conn, "Sum")

        # It has been graded: submissions, outcomes, test cases and two files
        # per case, which is the whole chain.
        paths = await query_challenge_file_paths(conn, hello)
        assert sorted(paths) == [f"Hello/in_{n}.txt" for n in range(3)] + [
            f"Hello/out_{n}.txt" for n in range(3)
        ]
        outcomes_before = (
            await conn.execute(
                select(func.count())
                .select_from(TestOutcome)
                .join(Submission, Submission.c.id == TestOutcome.c.submission_id)
                .where(Submission.c.challenge_id == hello)
            )
        ).scalar_one()
        assert outcomes_before > 0

        await delete_challenge(conn, hello)
        await conn.commit()

        for table, column in (
            (Challenge, Challenge.c.id),
            (TestCase, TestCase.c.challenge_id),
            (Submission, Submission.c.challenge_id),
        ):
            assert (
                await conn.execute(
                    select(func.count()).select_from(table).where(column == hello)
                )
            ).scalar_one() == 0

        # Its sibling on the same course is untouched, outcomes and all.
        assert (
            await conn.execute(
                select(func.count())
                .select_from(Submission)
                .where(Submission.c.challenge_id == summing)
            )
        ).scalar_one() > 0


async def test_deleting_a_course_leaves_nothing_behind(example_database, app_engine):
    engine = app_engine
    async with engine.connect() as conn:
        algorithms = await _course_id(conn, "Algorithms 101")
        systems = await _course_id(conn, "Systems Programming")
        doomed = await query_ordered_challenge_ids(conn, algorithms)
        assert len(doomed) == 2

        await delete_course(conn, algorithms)
        await conn.commit()

        assert not await course_exists(conn, algorithms)
        assert await query_ordered_challenge_ids(conn, algorithms) == []
        assert (
            await conn.execute(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.c.course_id == algorithms)
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(
                select(func.count())
                .select_from(Submission)
                .where(Submission.c.challenge_id.in_(doomed))
            )
        ).scalar_one() == 0

        # A sibling course keeps its challenges, its people and their work: a
        # delete names one course.
        assert await course_exists(conn, systems)
        assert len(await query_ordered_challenge_ids(conn, systems)) == 2
        assert (
            await conn.execute(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.c.course_id == systems)
            )
        ).scalar_one() == 3

        # And nobody lost their account with the course they were on.
        assert (
            await conn.execute(select(func.count()).select_from(User))
        ).scalar_one() == 6


async def test_the_lock_helpers_count_the_running_submissions(example_database, app_engine):
    """The count both danger zones refuse on. The *locking* half needs two
    connections racing each other and is documented on
    `lock_challenge_submissions` rather than tested here; what is asserted is
    that the answer is the same one `count_running_submissions` gives, so the
    stronger helper did not change the question."""
    engine = app_engine
    async with engine.connect() as conn:
        systems = await _course_id(conn, "Systems Programming")
        buffer_copy = await _challenge_id(conn, "Buffer copy")
        parallel_sum = await _challenge_id(conn, "Parallel sum")

        # dave's "Buffer copy" submission is the seeded `running` one.
        assert await lock_challenge_submissions(conn, buffer_copy) == 1
        assert await count_running_submissions(conn, buffer_copy) == 1
        # Queued is not running: it disappears before a judge ever pulls it.
        assert await lock_challenge_submissions(conn, parallel_sum) == 0

        # The course-wide one sums its challenges.
        assert await lock_course_submissions(conn, systems) == 1
        algorithms = await _course_id(conn, "Algorithms 101")
        assert await lock_course_submissions(conn, algorithms) == 0
        await conn.rollback()
