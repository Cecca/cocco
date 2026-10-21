"""Startup checks and the development seed data."""

from datetime import datetime

from sqlalchemy import func, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.avatar import generate_identicon
from cocco.migrate import (
    check_schema_is_current,
    reset_schema,
    upgrade_to_head,
)
from cocco.model import (
    Challenge,
    Course,
    OutcomeEnum,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    UserRole,
)
from cocco.repository.courses import insert_enrollment
from cocco.repository.lifecycle import set_challenge_archived
from cocco.repository.users import insert_administrators, insert_user
from cocco.storage import check_storage_format
from cocco.web import core
from cocco.web.core import engine

# The development cast, and the addresses `post_auth_dummy` is there to be
# typed. Matteo and Alice both teach every seeded course; what separates them is
# that Matteo is a site administrator - a row in `administrator`, not a role on
# any enrollment - so he may create courses and is staff on courses nobody
# enrolled him on. Bob and Charlie are students.
DUMMY_ADMIN = "matteo.ceccarello@unipd.it"
DUMMY_TEACHERS = ["alice@unipd.it"]
DUMMY_STAFF = [DUMMY_ADMIN, *DUMMY_TEACHERS]
DUMMY_STUDENTS = ["bob@unipd.it", "charlie@unipd.it"]


async def prepare_database(reset=False):
    """Check the schema, and only under `[dev] reset_db` build one.

    The schema is not created on boot any more - `cocco-migrate` owns it, and
    this refuses to start against a database that is behind, naming the command
    that fixes it. A server that quietly runs DDL is a server that can migrate a
    deployment nobody meant to migrate.

    `[dev] reset_db` is the one exception, because a reset is somebody asking
    for exactly that. It does not use `metadata.drop_all`: that drops the tables
    and the ENUM types with them, but `alembic_version` is not in
    `metadata` and would survive, so the `upgrade head` after it would find a
    database already claiming to be at head and do nothing - leaving an empty
    database that *passes* the check below and 500s on the first query. See
    `reset_schema`.
    """
    # Beside the schema check below and for the same reason, on the other half
    # of what a deployment has to bring forward: every file under the storage
    # root is compressed, and a root that predates that would fail one download
    # and one submission at a time instead of saying so once, here.
    check_storage_format(core.CONFIG.storage.root)

    if reset:
        async with engine.begin() as conn:
            await conn.run_sync(reset_schema)
        # A separate transaction: the drop is committed before alembic starts
        # its own work on the same connection.
        async with engine.begin() as conn:
            await conn.run_sync(upgrade_to_head)

    async with engine.begin() as conn:
        # Before anything is written, so that a database which is behind is
        # found out rather than inserted into. After a reset too, where it costs
        # one query and is the only thing proving the reset landed at head.
        await conn.run_sync(check_schema_is_current, process="cocco-web")
        if reset:
            await _populate_dummy_data(conn)
        # The configured administrators, replayed on every boot: this is how a
        # brand new deployment gets its first one, since there is no
        # administrator to appoint another. It only ever adds - see
        # `insert_administrators` - so a database that has moved on is left
        # alone, and deleting a line from `cocco.toml` demotes nobody.
        await insert_administrators(conn, core.CONFIG.auth.administrators)
        if core.DUMMY_AUTH:
            # `cocco.dev.toml` deliberately names no administrator - a file
            # every checkout shares should not make one person the
            # administrator of all of them - and the promise of dummy auth is
            # that the checkout is usable as it stands. Only DUMMY_ADMIN, so
            # that typing `alice@unipd.it` into the sign-in box still shows what
            # plain course staff see rather than quietly promoting her.
            await insert_administrators(conn, [DUMMY_ADMIN])


async def _seed_user(conn: AsyncConnection, email: str, nickname: str) -> int:
    image, image_mime = generate_identicon(nickname)
    return await insert_user(conn, email, nickname, image, image_mime)


async def _populate_dummy_data(conn: AsyncConnection):
    import textwrap

    user_ids = {
        email: await _seed_user(conn, email, email.split("@", 1)[0])
        for email in DUMMY_STAFF + DUMMY_STUDENTS
    }
    # The student whose attempts the seeded submission history belongs to.
    student_id = user_ids["bob@unipd.it"]

    course_titles = ["Algorithms 101", "Data Structures"]
    first_challenge_id: int | None = None
    last_challenge_id: int | None = None
    first_course_id: int | None = None
    shortest_route = textwrap.dedent("""\
        You are given a weighted, undirected graph of **city connections**.
        Compute the length of the shortest route between a source city and a
        destination city. If no route exists, return `-1`.

        ### Input

        First line: `n m` — number of cities and roads. Next `m` lines:
        `u v w`. Final line: `src dst`.

        ### Example

        ```
        5 6
        0 1 4
        0 2 1
        2 1 2
        1 3 1
        2 3 5
        3 4 3
        0 4
        ```

        The expected output is `7`.

        ### Constraints

        - 1 ≤ n ≤ 10⁴,  0 ≤ m ≤ 10⁵
        - 0 ≤ w ≤ 10³ · parallel edges allowed
        """)
    for course_title in course_titles:
        course_id = (
            await conn.execute(
                insert(Course)
                .values(title=course_title, description=textwrap.dedent("""\
                        Introduction to Algorithms. This course focuses on

                        - Honing algorithm design skills
                        - Introduce students to basic implementation techniques
                        """))
                .returning(Course.c.id)
            )
        ).scalar_one()
        if first_course_id is None:
            first_course_id = course_id
        # Matteo and Alice both teach; Bob and Charlie are students. `role` is
        # per course, so this is one row per person per course. What makes
        # Matteo an administrator is not written here but in `administrator`,
        # by the bootstrap in `prepare_database`.
        await insert_enrollment(
            conn, user_ids[DUMMY_ADMIN], course_id, UserRole.teacher
        )
        for email in DUMMY_TEACHERS:
            await insert_enrollment(
                conn, user_ids[email], course_id, UserRole.teacher
            )
        for email in DUMMY_STUDENTS:
            await insert_enrollment(
                conn, user_ids[email], course_id, UserRole.student
            )
        for n in range(3):
            first = first_challenge_id is None
            challenge_id = (
                await conn.execute(
                    insert(Challenge)
                    .values(
                        title=(
                            "Shortest Route Planner"
                            if first
                            else f"{course_title} - Challenge {n + 1}"
                        ),
                        description=(
                            shortest_route
                            if first
                            else f"challenge {n + 1} of {course_title}"
                        ),
                        course_id=course_id,
                        # Listed in the order they are created here; course
                        # staff reorder them with the arrows in the editor.
                        position=n,
                        # A CPython interpreter alone needs well over a few MB,
                        # so these have to be realistic or every run is killed.
                        max_memory_bytes=256 << 20,
                        max_time_s=5.0,
                        max_threads=1,
                        # No evaluator of its own: graded by the bundled
                        # default, exact match.
                        evaluator_path=None,
                        # Published. `insert_challenge` leaves this NULL - a new
                        # challenge is hidden - so a seed that did not say so
                        # would hand Bob and Charlie an empty course.
                        visible_from=func.now(),
                    )
                    .returning(Challenge.c.id)
                )
            ).scalar_one()
            if first_challenge_id is None:
                first_challenge_id = challenge_id
            last_challenge_id = challenge_id

    assert first_challenge_id is not None

    # One archived challenge inside a live course, and one archived course, so
    # both halves of archiving are visible in a fresh checkout without anybody
    # having to click. Alice is deliberately *not* enrolled on the archived
    # course: `[dev] dummy_user = "alice@unipd.it"` is then the view in which
    # an archived course is simply not listed, while Matteo - an administrator,
    # and so the only person who can unarchive one - finds it under "Other
    # courses", and Bob and Charlie see it in "Your courses", greyed and
    # read-only.
    from datetime import timedelta, timezone

    assert last_challenge_id is not None
    assert first_course_id is not None
    await set_challenge_archived(conn, last_challenge_id, True)
    archived_course_id = (
        await conn.execute(
            insert(Course)
            .values(
                title="Algorithms 101 (2024)",
                description=(
                    "Last year's edition, kept for the record. Enrolled "
                    "students can still read it and look back over their "
                    "submissions."
                ),
                archived_at=datetime.now(timezone.utc),
            )
            .returning(Course.c.id)
        )
    ).scalar_one()
    await insert_enrollment(
        conn, user_ids[DUMMY_ADMIN], archived_course_id, UserRole.teacher
    )
    for email in DUMMY_STUDENTS:
        await insert_enrollment(
            conn, user_ids[email], archived_course_id, UserRole.student
        )
    await conn.execute(
        insert(Challenge).values(
            title="Shortest Route Planner (2024)",
            description=shortest_route,
            course_id=archived_course_id,
            position=0,
            max_memory_bytes=256 << 20,
            max_time_s=5.0,
            max_threads=1,
            evaluator_path=None,
            visible_from=func.now(),
        )
    )

    # The two halves of visibility, on the first course, for archiving's reason:
    # a fresh checkout should show both without anybody having to click. One
    # challenge nobody but staff can see, and one that opens next week - which
    # Bob and Charlie find on their course page as a card with a date on it and
    # nothing to click.
    await conn.execute(
        insert(Challenge).values(
            title="Maximum Flow (draft)",
            description="Still being written. Students cannot see this one.",
            course_id=first_course_id,
            position=3,
            max_memory_bytes=256 << 20,
            max_time_s=5.0,
            max_threads=1,
            evaluator_path=None,
            visible_from=None,
        )
    )
    await conn.execute(
        insert(Challenge).values(
            title="Minimum Spanning Tree",
            description="Opens next week. Until then this is all anybody sees.",
            course_id=first_course_id,
            position=4,
            max_memory_bytes=256 << 20,
            max_time_s=5.0,
            max_threads=1,
            evaluator_path=None,
            visible_from=datetime.now(timezone.utc) + timedelta(days=7),
        )
    )

    # The first three are public samples, downloadable from the challenge page;
    # the rest are hidden. The last one is retired, so the editor shows both
    # states without anyone having to click first. Contents go on the
    # filesystem, not in the database (DESIGN.md, "Test case inputs"), so they
    # are written through `storage`.
    cases = [
        ("5 6\n0 1 4\n0 2 1\n2 1 2\n1 3 1\n2 3 5\n3 4 3\n0 4\n", "7\n", True, True),
        ("4 2\n0 1 5\n2 3 7\n0 3\n", "-1\n", True, True),
        (
            "6 9\n0 1 7\n0 2 9\n0 5 14\n1 2 10\n1 3 15\n2 3 11\n2 5 2\n"
            "3 4 6\n5 4 9\n0 4\n",
            "20\n",
            True,
            True,
        ),
        ("2 1\n0 1 3\n1 0\n", "3\n", False, True),
        ("3 3\n0 1 2\n1 2 2\n0 2 10\n0 2\n", "4\n", False, False),
    ]
    testcase_id: int | None = None
    for order, (test_input, expected, is_sample, is_active) in enumerate(cases):
        input_path = f"challenge_{first_challenge_id}/in_{order}.txt"
        output_path = f"challenge_{first_challenge_id}/out_{order}.txt"
        core.storage.write(input_path, test_input)
        core.storage.write(output_path, expected)
        tc_id = (
            await conn.execute(
                insert(TestCase)
                .values(
                    challenge_id=first_challenge_id,
                    execution_order=order,
                    input_path=input_path,
                    expected_output_path=output_path,
                    is_sample=is_sample,
                    is_active=is_active,
                )
                .returning(TestCase.c.id)
            )
        ).scalar_one()
        if testcase_id is None:
            testcase_id = tc_id

    assert testcase_id is not None

    # A plausible solution to the seeded challenge, so the submission page has
    # something worth highlighting in development.
    seeded_source = textwrap.dedent("""\
        import heapq
        import sys


        def shortest_route(n, edges, src, dst):
            \"\"\"Dijkstra over an undirected graph with non-negative weights.\"\"\"
            graph = [[] for _ in range(n)]
            for u, v, w in edges:
                graph[u].append((v, w))
                graph[v].append((u, w))

            dist = [float("inf")] * n
            dist[src] = 0
            queue = [(0, src)]
            while queue:
                d, u = heapq.heappop(queue)
                if u == dst:
                    return d
                if d > dist[u]:
                    continue  # a stale entry, already improved on
                for v, w in graph[u]:
                    if d + w < dist[v]:
                        dist[v] = d + w
                        heapq.heappush(queue, (dist[v], v))
            return -1


        def main():
            data = sys.stdin.read().split()
            n, m = int(data[0]), int(data[1])
            edges = [
                (int(data[2 + 3 * i]), int(data[3 + 3 * i]), int(data[4 + 3 * i]))
                for i in range(m)
            ]
            src, dst = int(data[2 + 3 * m]), int(data[3 + 3 * m])
            print(shortest_route(n, edges, src, dst))


        if __name__ == "__main__":
            main()
        """)

    async def _seed_submission(
        author_id: int,
        filename: str,
        outcome: OutcomeEnum,
        score: float,
        elapsed_s: float | None,
    ) -> None:
        submission_id = (
            await conn.execute(
                insert(Submission)
                .values(
                    user_id=author_id,
                    challenge_id=first_challenge_id,
                    status=SubmissionStatus.done,
                    language="python",
                    filename=filename,
                    source=seeded_source,
                )
                .returning(Submission.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(TestOutcome).values(
                submission_id=submission_id,
                testcase_id=testcase_id,
                outcome=outcome,
                score=score,
                elapsed_s=elapsed_s,
                peak_memory_bytes=12 << 20,
                message="ok" if outcome == OutcomeEnum.success else str(outcome.value),
                stdout="7\n" if outcome == OutcomeEnum.success else "",
                stderr="" if outcome == OutcomeEnum.success else "RuntimeError: boom",
            )
        )

    # A history of attempts for one student, so the submission page and the
    # per-student staff view have something to show.
    await _seed_submission(
        student_id, "first_try.py", OutcomeEnum.runtime_error, 0.0, None
    )
    await _seed_submission(student_id, "route_bfs.py", OutcomeEnum.success, 0.75, 0.412)
    await _seed_submission(
        student_id, "shortest_route.py", OutcomeEnum.success, 1.0, 0.238
    )
    await _seed_submission(
        user_ids["charlie@unipd.it"], "charlie.py", OutcomeEnum.success, 0.5, 0.310
    )

    # A few classmates, so the leaderboard has something to rank. They are not
    # enrolled: `query_challenge_standings` builds its cohort as the enrolled
    # students plus anyone who submitted, so they show up regardless.
    for nickname, filename, elapsed_s in [
        ("bianchi", "dijkstra.py", 0.121),
        ("rossi", "heapq_solution.py", 0.164),
        ("ferrari", "route.py", 0.199),
        ("romano", "planner.py", 0.402),
    ]:
        classmate_id = await _seed_user(conn, f"{nickname}@studenti.unipd.it", nickname)
        await _seed_submission(
            classmate_id, filename, OutcomeEnum.success, 1.0, elapsed_s
        )
