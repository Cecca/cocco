"""End-to-end tests of authoring an exercise set: importing the `.md` file,
watching its build, previewing it the way a student would, downloading it and
deleting it.

No judge runs here. A build is finished by hand with the judge's own
`finish_version_build`, and a job with `finish_exercise_job`, so what is under
test is the web half - what an import writes, in one transaction, and what the
pages make of what the judge wrote. The judge half is `test_exercise_judge.py`.
"""

import re
import textwrap

import pytest
from sqlalchemy import func, insert, select

from cocco.repository import judging as repo
from cocco.conftest import sign_in_as
from cocco.exercises import format as exercise_format
from cocco.model import (
    Administrator,
    Course,
    Enrollment,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSessionMode,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
    User,
    UserRole,
)
from cocco.schema import ExerciseCheckResult, ExerciseRunResult, ExerciseTestResult

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
OTHER_TEACHER = "dave@unipd.it"
ADMIN = "matteo.ceccarello@unipd.it"

SOURCE = textwrap.dedent(
    '''\
    ---
    title: Warm-up
    ---

    # Double

    Print twice the number.

    ```starter
    n = int(input())
    ```

    ```solution
    n = int(input("n? "))
    print(2 * n)
    ```

    ```input
    21
    ```

    ```input hidden
    5
    ```

    ```hint
    Multiply.
    ```

    # Shout

    Shout it back.

    ```solution
    print(input().upper())
    ```

    ```input
    hi
    ```
    '''
)

# An `input` with nothing to compute its output from: an error at parse time.
UNPARSEABLE = "---\ntitle: Broken\n---\n\n# A\n\nStatement.\n\n```input\n1\n```\n"


@pytest.fixture
def world(db):
    """Alice teaches the course, Bob studies on it, Dave teaches another one,
    and Matteo administers the site."""
    ids = {}
    with db.begin() as conn:
        for email, nickname in (
            (TEACHER, "alice"),
            (STUDENT, "bob"),
            (OTHER_TEACHER, "dave"),
            (ADMIN, "matteo"),
        ):
            ids[nickname] = conn.execute(
                insert(User)
                .values(email=email, nickname=nickname, image=b"", image_mime="image/svg+xml")
                .returning(User.c.id)
            ).scalar_one()
        for key, title in (("course", "Programming 1"), ("other", "Databases")):
            ids[key] = conn.execute(
                insert(Course).values(title=title, description="...").returning(Course.c.id)
            ).scalar_one()
        for nickname, course, role in (
            ("alice", "course", UserRole.teacher),
            ("bob", "course", UserRole.student),
            ("dave", "other", UserRole.teacher),
        ):
            conn.execute(
                insert(Enrollment).values(
                    user_id=ids[nickname], course_id=ids[course], role=role
                )
            )
        conn.execute(insert(Administrator).values(email=ADMIN))
    return ids


@pytest.fixture
def client(make_client, world):
    return make_client(as_=TEACHER)


def _import(client, course_id, source=SOURCE, filename="warm-up.md", set_id=None):
    url = (
        f"/courses/{course_id}/exercises"
        if set_id is None
        else f"/courses/{course_id}/exercises/{set_id}/import"
    )
    body = source.encode() if isinstance(source, str) else source
    return client.post(
        url, files={"exercise_file": (filename, body, "text/markdown")}
    )


def _imported(client, world) -> int:
    """Import `SOURCE` as a new set, and answer the set's id."""
    response = _import(client, world["course"])
    assert response.status_code == 204, response.text
    return int(re.search(r"/exercises/(\d+)$", response.headers["HX-Redirect"])[1])


def _count(db, table, *where) -> int:
    with db.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(table).where(*where)
        ).scalar_one()


def _versions(db, set_id):
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseSetVersion)
            .where(ExerciseSetVersion.c.set_id == set_id)
            .order_by(ExerciseSetVersion.c.id)
        ).all()


def _build(db, version_id, source=SOURCE) -> None:
    """Finish a version's build the way the judge does, with expected outputs
    a solution would have printed."""
    document = exercise_format.parse(source).document
    for exercise in document.exercises:
        for test in exercise.tests:
            test.expected_output = "42\n" if exercise.title == "Double" else "HI\n"
    with db.begin() as conn:
        repo.finish_version_build(
            conn,
            version_id,
            ExerciseVersionStatus.ready,
            [],
            document.model_dump(mode="json"),
        )


def _jobs(db, *where):
    """The preview jobs - every job but the builds the imports queued."""
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseJob)
            .where(ExerciseJob.c.kind != ExerciseJobKind.build, *where)
            .order_by(ExerciseJob.c.id)
        ).all()


########################################################################
# Importing


async def test_an_import_creates_the_set_its_version_and_the_build(client, world, db):
    """One transaction: the set, a `building` version holding the file
    verbatim and titled from its front matter, and a build job - and the
    browser is sent to the set's page to watch it."""
    set_id = _imported(client, world)

    (version,) = _versions(db, set_id)
    assert version.source == SOURCE
    assert version.title == "Warm-up"
    assert version.status == ExerciseVersionStatus.building
    with db.connect() as conn:
        (job,) = conn.execute(
            select(ExerciseJob).where(ExerciseJob.c.version_id == version.id)
        ).all()
    assert job.kind == ExerciseJobKind.build
    assert job.status == ExerciseJobStatus.queued
    assert job.user_id == world["alice"]
    assert job.session_id is None


async def test_the_file_name_titles_a_set_whose_file_does_not(client, world, db):
    untitled = SOURCE.replace("---\ntitle: Warm-up\n---\n", "")
    response = _import(client, world["course"], untitled, filename="Loops and lists.md")
    set_id = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    assert _versions(db, set_id)[0].title == "Loops and lists"


@pytest.mark.parametrize(
    "body, filename, sentence",
    [
        (UNPARSEABLE, "broken.md", "The file has errors"),
        (b"\xff\xfe not text", "binary.md", "not valid UTF-8"),
        (b"x" * (300 << 10), "huge.md", "larger than 256 KB"),
    ],
    ids=["parse error", "not utf-8", "too big"],
)
async def test_a_refused_import_creates_nothing(client, world, db, body, filename, sentence):
    """200 with the alert, htmx's rule, and not a row anywhere: a refused file
    makes no set, no version and no job."""
    response = _import(client, world["course"], body, filename)
    assert response.status_code == 200
    assert sentence in response.text
    for table in (ExerciseSet, ExerciseSetVersion, ExerciseJob):
        assert _count(db, table) == 0


async def test_a_parse_error_lists_the_problems(client, world):
    response = _import(client, world["course"], UNPARSEABLE)
    assert 'The "solution" block is missing' in response.text


async def test_a_reimport_is_a_new_version_of_the_same_set(client, world, db):
    """The current version does not move: only a build that finishes ready
    moves it, so a re-import cannot break what works."""
    set_id = _imported(client, world)
    first = _versions(db, set_id)[0].id
    _build(db, first)

    response = _import(client, world["course"], SOURCE + "\n", set_id=set_id)
    assert response.status_code == 200
    assert "Imported" in response.text
    # The status comes along out of band, and polls itself: the new version
    # is building.
    assert 'hx-swap-oob="true"' in response.text
    assert 'hx-trigger="every 2s"' in response.text

    versions = _versions(db, set_id)
    assert [v.status for v in versions] == [
        ExerciseVersionStatus.ready,
        ExerciseVersionStatus.building,
    ]
    assert _count(db, ExerciseJob, ExerciseJob.c.version_id == versions[1].id) == 1
    with db.connect() as conn:
        current = conn.execute(
            select(ExerciseSet.c.current_version_id).where(ExerciseSet.c.id == set_id)
        ).scalar_one()
    assert current == first


async def test_a_reimport_with_errors_adds_no_version(client, world, db):
    set_id = _imported(client, world)
    response = _import(client, world["course"], UNPARSEABLE, set_id=set_id)
    assert response.status_code == 200
    assert "no version was created" in response.text
    assert len(_versions(db, set_id)) == 1


########################################################################
# Who may


async def test_exercise_sets_are_course_staff_only(client, world, db):
    """`CourseEditor` on every route, reads included: the source carries the
    solutions."""
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id
    _build(db, version_id)
    course = world["course"]
    base = f"/courses/{course}/exercises/{set_id}"

    sign_in_as(client, STUDENT)
    for url in (
        base,
        f"/f/courses/{course}/exercises/{set_id}",
        f"{base}/versions/{version_id}/source",
        f"{base}/versions/{version_id}/preview/1",
    ):
        assert client.get(url).status_code == 403, url
    assert _import(client, course).status_code == 403
    assert client.post(f"{base}/versions/{version_id}/preview/1/run").status_code == 403
    assert client.post(f"{base}/delete", data={"confirm": "Warm-up"}).status_code == 403
    assert "Exercises" not in client.get(f"/courses/{course}").text


async def test_a_set_is_reached_only_through_its_own_course(client, world, db):
    """Dave teaches the other course: naming it in front of this course's set
    is a set that does not exist there, and naming this course is not his."""
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id

    sign_in_as(client, OTHER_TEACHER)
    other = world["other"]
    assert client.get(f"/courses/{other}/exercises/{set_id}").status_code == 404
    assert (
        client.get(f"/courses/{other}/exercises/{set_id}/versions/{version_id}/source")
        .status_code == 404
    )
    assert client.get(f"/courses/{world['course']}/exercises/{set_id}").status_code == 403


async def test_an_administrator_is_staff_on_every_set(client, world, db):
    set_id = _imported(client, world)
    sign_in_as(client, ADMIN)
    assert client.get(f"/courses/{world['course']}/exercises/{set_id}").status_code == 200


########################################################################
# The pages


async def test_the_course_page_lists_the_sets_for_staff(client, world, db):
    set_id = _imported(client, world)
    page = client.get(f"/courses/{world['course']}").text
    assert "Exercises" in page
    assert "Warm-up" in page
    assert f"/exercises/{set_id}" in page


async def test_the_status_polls_while_building_and_stops_when_built(client, world, db):
    set_id = _imported(client, world)
    url = f"/f/courses/{world['course']}/exercises/{set_id}"
    building = client.get(url)
    assert building.status_code == 200
    assert 'hx-trigger="every 2s"' in building.text

    _build(db, _versions(db, set_id)[0].id)
    built = client.get(url).text
    assert 'hx-trigger="every 2s"' not in built
    assert "current" in built
    assert "Preview" in built


async def test_a_failed_build_shows_its_problems_and_no_preview(client, world, db):
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id
    with db.begin() as conn:
        repo.finish_version_build(
            conn,
            version_id,
            ExerciseVersionStatus.failed,
            [
                {
                    "level": "error",
                    "where": 'exercise 1, "Double"',
                    "text": "The solution raised on test 2.",
                    "exercise": 1,
                    "test": 2,
                }
            ],
            None,
        )
    page = client.get(f"/courses/{world['course']}/exercises/{set_id}").text
    assert "The solution raised on test 2." in page
    assert "test 2" in page
    assert "/preview/" not in page
    assert (
        client.get(
            f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/preview/1"
        ).status_code
        == 404
    )


async def test_the_source_downloads_verbatim(client, world, db):
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id
    response = client.get(
        f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/source"
    )
    assert response.status_code == 200
    assert response.text == SOURCE
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == 'attachment; filename="warm-up-v1.md"'
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_version_of_another_set_is_not_found(client, world, db):
    """The set is part of every version's key: a version id from a second set
    of the same course is not this set's."""
    first = _imported(client, world)
    second = _imported(client, world)
    version_of_second = _versions(db, second)[0].id
    url = f"/courses/{world['course']}/exercises/{first}/versions/{version_of_second}/source"
    assert client.get(url).status_code == 404


########################################################################
# The preview


@pytest.fixture
def ready(client, world, db):
    """A set whose first version has been built: (set_id, version_id, base url)."""
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id
    _build(db, version_id)
    base = f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/preview"
    return set_id, version_id, base


async def test_the_preview_shows_staff_what_a_student_never_sees(client, ready):
    _, _, base = ready
    page = client.get(f"{base}/1")
    assert page.status_code == 200
    assert "Print twice the number." in page.text
    assert "n = int(input())" in page.text  # the starter, in the editor
    assert "print(2 * n)" in page.text  # the solution
    assert "hidden · staff only" in page.text
    assert client.get(f"{base}/3").status_code == 404
    assert client.get(f"{base}/0").status_code == 404


async def test_a_preview_run_is_a_job_without_a_session(client, world, db, ready):
    _, version_id, base = ready
    response = client.post(f"{base}/1/run", data={"source": "print(1)", "stdin": "7"})
    assert response.status_code == 200
    (job,) = _jobs(db)
    assert job.kind == ExerciseJobKind.run
    assert job.session_id is None
    assert job.version_id == version_id
    assert job.exercise_index == 1
    assert (job.source, job.stdin) == ("print(1)", "7")
    # Pending, so the fragment polls itself.
    assert 'hx-trigger="every 500ms"' in response.text
    assert f"/jobs/{job.id}" in response.text


async def test_pressing_the_same_button_again_replaces_the_queued_job(client, db, ready):
    """`uq_exercise_job_one_queued`, upserted: the same id, the newer code. A
    Check is another button, and so another job."""
    _, _, base = ready
    client.post(f"{base}/1/run", data={"source": "print(1)", "stdin": ""})
    client.post(f"{base}/1/run", data={"source": "print(2)", "stdin": "x"})
    client.post(f"{base}/1/check", data={"source": "print(3)"})
    run, check = _jobs(db)
    assert (run.kind, run.source, run.stdin) == (ExerciseJobKind.run, "print(2)", "x")
    assert (check.kind, check.source, check.stdin) == (ExerciseJobKind.check, "print(3)", None)

    # A job the judge has claimed is not replaced: the next click queues anew.
    with db.begin() as conn:
        conn.execute(
            ExerciseJob.update()
            .where(ExerciseJob.c.id == run.id)
            .values(status=ExerciseJobStatus.running, started_at=func.now())
        )
    client.post(f"{base}/1/run", data={"source": "print(4)", "stdin": ""})
    assert [j.source for j in _jobs(db, ExerciseJob.c.kind == ExerciseJobKind.run)] == [
        "print(2)",
        "print(4)",
    ]


async def test_too_much_code_is_refused_with_an_alert(client, db, ready):
    _, _, base = ready
    response = client.post(f"{base}/1/run", data={"source": "#" * (65 << 10)})
    assert response.status_code == 200
    assert "larger than 64 KB" in response.text
    assert _jobs(db) == []


async def test_a_done_check_shows_every_test_and_stops_polling(client, world, db, ready):
    set_id, _, base = ready
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    (job,) = _jobs(db)
    result = ExerciseCheckResult(
        passed=False,
        tests=[
            ExerciseTestResult(status="ok", stdout="42\n", verdict="pass", hidden=False),
            ExerciseTestResult(
                status="ok", stdout="41\n", verdict="fail", hidden=True, diff=[0]
            ),
        ],
    )
    with db.begin() as conn:
        conn.execute(
            ExerciseJob.update()
            .where(ExerciseJob.c.id == job.id)
            .values(status=ExerciseJobStatus.running, started_at=func.now())
        )
        repo.finish_exercise_job(conn, job.id, result.model_dump(mode="json"))

    fragment = client.get(f"/f/courses/{world['course']}/exercises/{set_id}/jobs/{job.id}")
    assert fragment.status_code == 200
    assert "every 500ms" not in fragment.text
    assert "1 of 2 tests passed." in fragment.text
    # Staff see the hidden test's output in full, flagged line and all.
    assert "hidden" in fragment.text
    assert "41" in fragment.text
    assert "bg-error/15" in fragment.text


async def test_a_done_run_shows_its_transcript(client, world, db, ready):
    set_id, _, base = ready
    client.post(f"{base}/2/run", data={"source": "x", "stdin": "hi"})
    (job,) = _jobs(db)
    result = ExerciseRunResult(
        status="ok",
        stdout="hi\n",
        exception={
            "type": "NameError",
            "mro": ["NameError", "Exception", "BaseException", "object"],
            "line": 1,
            "traceback": "Traceback ...",
        },
    )
    with db.begin() as conn:
        conn.execute(
            ExerciseJob.update()
            .where(ExerciseJob.c.id == job.id)
            .values(status=ExerciseJobStatus.running, started_at=func.now())
        )
        repo.finish_exercise_job(conn, job.id, result.model_dump(mode="json"))
    text = client.get(f"/f/courses/{world['course']}/exercises/{set_id}/jobs/{job.id}").text
    assert "Transcript" in text
    assert "NameError" in text
    assert "Line 1: name not defined" in text


async def test_a_preview_job_is_its_askers_own(client, world, db, ready):
    """Matteo is staff here too, and still does not find Alice's job at its
    URL: a polling tab never finds somebody else's job in its place."""
    set_id, _, base = ready
    client.post(f"{base}/1/run", data={"source": "print(1)"})
    (job,) = _jobs(db)
    url = f"/f/courses/{world['course']}/exercises/{set_id}/jobs/{job.id}"
    assert client.get(url).status_code == 200
    sign_in_as(client, ADMIN)
    assert client.get(url).status_code == 404


########################################################################
# Deleting


def _open_session(db, set_id, version_id) -> int:
    with db.begin() as conn:
        return conn.execute(
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


async def test_deleting_a_set_takes_its_versions_and_jobs(client, world, db, ready):
    """The cascade by `version_id`: the build and the preview have no session,
    and would otherwise hold the versions down. A sibling set is untouched."""
    set_id, _, base = ready
    sibling = _imported(client, world)
    client.post(f"{base}/1/run", data={"source": "print(1)"})
    _import(client, world["course"], set_id=set_id)  # a second, building version

    response = client.post(
        f"/courses/{world['course']}/exercises/{set_id}/delete",
        data={"confirm": "Warm-up"},
    )
    assert response.status_code == 204
    assert response.headers["HX-Redirect"].endswith(f"/courses/{world['course']}")
    assert _count(db, ExerciseSet, ExerciseSet.c.id == set_id) == 0
    assert _versions(db, set_id) == []
    sibling_versions = _versions(db, sibling)
    assert len(sibling_versions) == 1
    with db.connect() as conn:
        left = conn.execute(select(ExerciseJob.c.version_id, ExerciseJob.c.kind)).all()
    assert left == [(sibling_versions[0].id, ExerciseJobKind.build)]


async def test_deleting_a_set_asks_for_its_title(client, world, db, ready):
    set_id, _, _ = ready
    response = client.post(
        f"/courses/{world['course']}/exercises/{set_id}/delete",
        data={"confirm": "warm-up"},
    )
    assert response.status_code == 200
    assert "Nothing was changed" in response.text
    assert _count(db, ExerciseSet) == 1


async def test_a_set_with_a_session_is_not_deleted(client, world, db, ready):
    set_id, version_id, _ = ready
    _open_session(db, set_id, version_id)
    response = client.post(
        f"/courses/{world['course']}/exercises/{set_id}/delete",
        data={"confirm": "Warm-up"},
    )
    assert response.status_code == 200
    assert "1 session" in response.text
    assert _count(db, ExerciseSet) == 1
    assert len(_versions(db, set_id)) == 1


########################################################################
# Review follow-ups


def _fail(db, version_id) -> None:
    with db.begin() as conn:
        repo.finish_version_build(conn, version_id, ExerciseVersionStatus.failed, [], None)


async def test_a_failed_reimport_does_not_rename_the_set(client, world, db):
    """The set is called what its current version is called: a broken file
    breaks nothing, its name included - the delete dialog's included."""
    set_id = _imported(client, world)
    _build(db, _versions(db, set_id)[0].id)
    renamed = SOURCE.replace("title: Warm-up", "title: Renamed")
    _import(client, world["course"], renamed, set_id=set_id)
    _fail(db, _versions(db, set_id)[1].id)

    assert "Renamed" not in client.get(f"/courses/{world['course']}").text
    page = client.get(f"/courses/{world['course']}/exercises/{set_id}").text
    assert 'data-expected="Warm-up"' in page
    response = client.post(
        f"/courses/{world['course']}/exercises/{set_id}/delete",
        data={"confirm": "Warm-up"},
    )
    assert response.status_code == 204


async def test_a_set_with_no_current_version_is_named_by_its_latest(client, world, db):
    set_id = _imported(client, world)
    _fail(db, _versions(db, set_id)[0].id)
    assert "Warm-up" in client.get(f"/courses/{world['course']}").text


async def test_a_reimports_alert_sits_outside_the_polled_status(client, world, db):
    """A refused file's problems are not inside the element that polls - the
    next poll would wipe them while an earlier version is still building."""
    set_id = _imported(client, world)  # v1, still building
    response = _import(client, world["course"], UNPARSEABLE, set_id=set_id)
    text = response.text
    status_at = text.index('id="exercise-set-status"')
    assert text.index("no version was created") < status_at
    assert text.index('The "solution" block is missing') < status_at
    assert "no version was created" not in text[status_at:]


async def test_the_status_polls_while_any_version_is_building(client, world, db):
    """v1 still building, v2 already built: two builds finish in either
    order, and the older one finishing is news too."""
    set_id = _imported(client, world)
    _import(client, world["course"], SOURCE + "\n", set_id=set_id)
    _build(db, _versions(db, set_id)[1].id)
    url = f"/f/courses/{world['course']}/exercises/{set_id}"
    assert 'hx-trigger="every 2s"' in client.get(url).text
    _build(db, _versions(db, set_id)[0].id)
    assert 'hx-trigger="every 2s"' not in client.get(url).text


@pytest.mark.parametrize(
    "kind, data",
    [
        ("run", {"source": "print(1)\x00", "stdin": ""}),
        ("run", {"source": "print(1)", "stdin": "a\x00b"}),
        ("check", {"source": "\x00"}),
    ],
    ids=["run code", "run stdin", "check code"],
)
async def test_a_nul_is_refused_with_an_alert(client, db, ready, kind, data):
    """PostgreSQL cannot store one: refused before the insert, not a 500."""
    _, _, base = ready
    response = client.post(f"{base}/1/{kind}", data=data)
    assert response.status_code == 200
    assert "NUL character" in response.text
    assert _jobs(db) == []


async def test_a_pending_job_replaces_itself_on_each_poll(client, world, db, ready):
    set_id, _, base = ready
    client.post(f"{base}/1/run", data={"source": "print(1)"})
    (job,) = _jobs(db)
    text = client.get(f"/f/courses/{world['course']}/exercises/{set_id}/jobs/{job.id}").text
    assert 'hx-trigger="every 500ms"' in text
    assert 'hx-swap="outerHTML"' in text


async def test_every_set_route_is_404_under_another_course(client, world, db, ready):
    """Dave is staff on the other course, and naming it in front of this
    course's set gets him nowhere - reads, writes and the job fragment."""
    set_id, version_id, _ = ready
    client.post(
        f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/preview/1/run",
        data={"source": "print(1)"},
    )
    (job,) = _jobs(db)

    sign_in_as(client, OTHER_TEACHER)
    other = world["other"]
    base = f"/courses/{other}/exercises/{set_id}"
    assert _import(client, other, set_id=set_id).status_code == 404
    for kind in ("run", "check"):
        url = f"{base}/versions/{version_id}/preview/1/{kind}"
        assert client.post(url, data={"source": "x"}).status_code == 404, url
    assert client.post(f"{base}/delete", data={"confirm": "Warm-up"}).status_code == 404
    assert client.get(f"/f/courses/{other}/exercises/{set_id}/jobs/{job.id}").status_code == 404
    assert len(_versions(db, set_id)) == 1
    assert len(_jobs(db)) == 1


async def test_a_job_is_not_found_under_another_set(client, world, db, ready):
    set_id, _, base = ready
    sibling = _imported(client, world)
    client.post(f"{base}/1/run", data={"source": "print(1)"})
    (job,) = _jobs(db)
    course = world["course"]
    assert client.get(f"/f/courses/{course}/exercises/{set_id}/jobs/{job.id}").status_code == 200
    assert client.get(f"/f/courses/{course}/exercises/{sibling}/jobs/{job.id}").status_code == 404


async def test_a_sessions_check_is_not_readable_as_a_preview(client, world, db, ready):
    """A recorded check of Alice's own, in a session: the preview fragment is
    for jobs without one, so it is not found there."""
    set_id, version_id, _ = ready
    session_id = _open_session(db, set_id, version_id)
    with db.begin() as conn:
        job_id = conn.execute(
            insert(ExerciseJob)
            .values(
                kind=ExerciseJobKind.check,
                status=ExerciseJobStatus.queued,
                user_id=world["alice"],
                version_id=version_id,
                session_id=session_id,
                exercise_index=1,
                source="print(1)",
            )
            .returning(ExerciseJob.c.id)
        ).scalar_one()
    url = f"/f/courses/{world['course']}/exercises/{set_id}/jobs/{job_id}"
    assert client.get(url).status_code == 404


async def test_an_import_into_a_set_deleted_meanwhile_is_404(client, world, db, monkeypatch):
    """The set vanished between the dependency's check and the insert: its
    foreign key refuses the version, and the answer is the 404 any later
    request would get - not a 500."""
    import cocco.main as main

    async def always(conn, set_id, course_id):
        return True

    monkeypatch.setattr(main, "exercise_set_in_course", always)
    response = _import(client, world["course"], set_id=999)
    assert response.status_code == 404
    assert _count(db, ExerciseSetVersion) == 0


async def test_a_preview_on_a_set_deleted_meanwhile_is_404(client, world, db, ready, monkeypatch):
    """The same race for a preview: the version was read, then deleted, and
    the job's foreign key refuses it."""
    import cocco.main as main

    _, _, base = ready
    real = main.query_exercise_version

    async def read_then_delete(conn, set_id, version_id):
        version = await real(conn, set_id, version_id)
        with db.begin() as other:
            other.execute(ExerciseJob.delete())
            other.execute(ExerciseSet.update().values(current_version_id=None))
            other.execute(ExerciseSetVersion.delete())
        return version

    monkeypatch.setattr(main, "query_exercise_version", read_then_delete)
    response = client.post(f"{base}/1/run", data={"source": "print(1)"})
    assert response.status_code == 404
