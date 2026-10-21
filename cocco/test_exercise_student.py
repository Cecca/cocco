"""The student page of an exercise session: the per-exercise page and its
wait page, the strip, Run and Check, the result fragment - and the functions
that decide what of the compiled document and of a check's result a student
is handed.

The set, the session and the people are `test_exercise_sessions.py`'s. No
judge runs: a job is finished by hand with the judge's `finish_exercise_job`.
"""

import re

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from cocco import main
from cocco.web import exercise_sessions, exercise_sets
from cocco.repository import judging as repo
from cocco.conftest import sign_in_as
from cocco.exercises import format as exercise_format
from cocco.model import (
    Enrollment,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSessionParticipant,
    User,
    UserRole,
)
from cocco.schema import (
    ExerciseCheckResult,
    ExerciseRunResult,
    ExerciseTestResult,
    HarnessException,
)
from cocco.test_exercise_routes import (  # noqa: F401 - fixtures
    ADMIN,
    SOURCE,
    STUDENT,
    TEACHER,
    _count,
    client,
    ready,
    world,
)
from cocco.test_exercise_sessions import (  # noqa: F401 - fixtures
    LONGER,
    _opened,
    _reimport,
    session,
)


def _exercise(title="Double"):
    """SOURCE's exercise, as the build leaves it: expected outputs filled in."""
    document = exercise_format.parse(SOURCE).document
    exercise = next(e for e in document.exercises if e.title == title)
    for test in exercise.tests:
        test.expected_output = "42\n"
    return exercise


def _jobs(db, *where):
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseJob)
            .where(ExerciseJob.c.kind != ExerciseJobKind.build, *where)
            .order_by(ExerciseJob.c.id)
        ).all()


def _finish(db, job_id, result) -> None:
    with db.begin() as conn:
        conn.execute(
            ExerciseJob.update()
            .where(ExerciseJob.c.id == job_id)
            .values(status=ExerciseJobStatus.running, started_at=func.now())
        )
        repo.finish_exercise_job(conn, job_id, result.model_dump(mode="json"))


def _participants(db, session_id) -> list[int]:
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseSessionParticipant.c.user_id).where(
                ExerciseSessionParticipant.c.session_id == session_id
            )
        ).scalars().all()


def _user_id(db, email) -> int:
    with db.connect() as conn:
        return conn.execute(select(User.c.id).where(User.c.email == email)).scalar_one()


# A check of SOURCE's "Double": the visible test passed, the hidden one raised
# an exception whose class is named after the hidden input, and printed it.
SECRET_CHECK = ExerciseCheckResult(
    passed=False,
    tests=[
        ExerciseTestResult(status="ok", stdout="42\n", verdict="pass", hidden=False),
        ExerciseTestResult(
            status="ok",
            elapsed_s=1.23,
            stdout="the input was 5-SECRET\n",
            stderr="5-SECRET on stderr",
            exit=5,
            verdict="error",
            hidden=True,
            exception={
                "type": "Secret5",
                "mro": ["Secret5", "ValueError", "Exception", "BaseException", "object"],
                "line": 3,
                "traceback": "Traceback: 5-SECRET",
            },
        ),
    ],
)


########################################################################
# What leaves the server


def test_the_student_view_has_no_hidden_test_and_no_unrevealed_solution():
    exercise = _exercise()
    view = main.student_exercise_view(exercise, revealed=False)
    assert set(view) == {
        "title", "statement", "starter", "hints", "rules", "time_s", "tests",
        "n_hidden", "solution",
    }
    # SOURCE's "Double" has a visible test (21) and a hidden one (5).
    assert [t["input"] for t in view["tests"]] == ["21\n"]
    assert view["tests"][0]["expected_output"] == "42\n"
    assert view["n_hidden"] == 1
    assert view["solution"] is None
    assert view["hints"] == ["Multiply."]
    assert "5\n" not in repr(view)

    revealed = main.student_exercise_view(exercise, revealed=True)
    assert revealed["solution"] == exercise.solution


def test_a_hidden_test_is_only_its_verdict_and_its_exception():
    rows = exercise_sets.exercise_check_rows(SECRET_CHECK, _exercise(), redact=True)
    visible, hidden = rows
    assert visible["redacted"] is False and visible["test"].input == "21\n"
    # No line and no time: both are numbers the program can choose.
    assert hidden == {
        "n": 2,
        "verdict": "error",
        "hidden": True,
        "redacted": True,
        # The class is the student's to name; its nearest builtin is not.
        "exception": {"type": "ValueError"},
    }
    assert "SECRET" not in repr(rows)


def test_staff_see_a_hidden_test_in_full():
    _, hidden = exercise_sets.exercise_check_rows(SECRET_CHECK, _exercise(), redact=False)
    assert hidden["redacted"] is False
    assert hidden["result"].stdout == "the input was 5-SECRET\n"
    assert hidden["test"].input == "5\n"


def test_a_test_the_version_does_not_have_is_redacted():
    """Nothing says it is visible, so it is not shown as if it were."""
    exercise = _exercise()
    exercise.tests = exercise.tests[:0]
    rows = exercise_sets.exercise_check_rows(SECRET_CHECK, exercise, redact=True)
    assert all(row["redacted"] for row in rows)


def test_a_diff_past_a_clipped_output_marks_only_what_is_there():
    result = ExerciseCheckResult(
        passed=False,
        tests=[
            ExerciseTestResult(
                status="ok", stdout="1\n2\n", clipped=True, verdict="fail",
                hidden=False, diff=[1, 7, 900],
            )
        ],
    )
    (row,) = exercise_sets.exercise_check_rows(result, _exercise(), redact=True)
    assert row["actual"] == [("1", False), ("2", True)]
    # Index 1 of "42\n" is the line after it: the expected output's own index.
    assert row["expected"] == [("42", False), ("", True)]


def test_the_strip_hides_the_titles_past_the_pacing():
    strip = {
        "mode": "live",
        "unlocked_through": 2,
        "closed_at": None,
        "titles": ["One", "Two", "Three"],
        "checks": [
            {"n": 1, "solved": True, "checking": True},
            {"n": 2, "solved": False, "checking": False},
            {"n": 3, "solved": True, "checking": False},
        ],
    }
    view = main.session_strip_view(strip, UserRole.student)
    assert view == {
        "exercises": [
            {"n": 1, "title": "One", "status": "solved", "checking": True},
            {"n": 2, "title": "Two", "status": "attempted", "checking": False},
        ],
        "n_locked": 1,
    }
    staff = main.session_strip_view(strip, UserRole.teacher)
    assert staff["n_locked"] == 0 and staff["exercises"][2]["title"] == "Three"
    open_ = main.session_strip_view({**strip, "mode": "open", "checks": []}, UserRole.student)
    assert [e["status"] for e in open_["exercises"]] == ["not_attempted"] * 3


########################################################################
# The page


async def test_the_student_page_shows_the_exercise_and_registers_them(
    client, world, db, session
):
    session_id, base = session
    sign_in_as(client, STUDENT)
    page = client.get(f"{base}/1")
    assert page.status_code == 200
    assert "Print twice the number." in page.text
    assert "n = int(input())" in page.text  # the starter, in the editor
    assert "Plus 1 hidden" in page.text
    assert "print(2 * n)" not in page.text  # the solution, not revealed
    assert "codemirror/codemirror.js?v=" in page.text
    assert f'data-draft-key="cocco:exercise:{world["bob"]}:{session_id}:1"' in page.text
    assert 'hx-trigger="every 3s"' in page.text  # the strip
    assert _participants(db, session_id) == [world["bob"]]
    client.get(f"{base}/1")
    assert _participants(db, session_id) == [world["bob"]]


async def test_staff_are_not_participants_and_queue_nothing(client, world, db, ready, session):
    """Staff open the page past the pacing, and are not counted; their Run and
    Check are refused with a link to the preview, so a session's jobs are all
    its students' work."""
    set_id, version_id, _ = ready
    session_id, base = session
    page = client.get(f"{base}/2")
    assert page.status_code == 200
    assert "This is the student page" in page.text
    assert 'id="check-button"' not in page.text and 'id="run-button"' not in page.text
    preview = f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/preview/2"
    assert preview in page.text
    assert _participants(db, session_id) == []
    for kind in ("run", "check"):
        response = client.post(f"{base}/2/{kind}", data={"source": "print(1)"})
        assert response.status_code == 200
        assert "You are staff on this course" in response.text
        assert preview in response.text
    sign_in_as(client, ADMIN)
    response = client.post(f"{base}/1/check", data={"source": "print(1)"})
    assert "You are staff on this course" in response.text
    assert _jobs(db) == []
    # No link to the preview of an exercise the version does not have.
    assert client.post(f"{base}/9/check", data={"source": "print(1)"}).status_code == 404


async def test_a_participant_whose_session_vanished_still_gets_an_answer(
    client, session, monkeypatch
):
    """The insert's foreign key refusing - the session deleted since the gate
    found it - is rolled back, not a 500."""
    _, base = session

    async def gone(*args):
        raise IntegrityError("INSERT", {}, Exception("fk"))

    monkeypatch.setattr(exercise_sessions, "insert_session_participant", gone)
    sign_in_as(client, STUDENT)
    assert client.get(f"{base}/1").status_code == 200


async def test_a_closed_session_registers_nobody_and_takes_nothing(client, world, db, session):
    session_id, base = session
    client.post(f"{base}/close")
    sign_in_as(client, STUDENT)
    page = client.get(f"{base}/1")
    assert page.status_code == 200
    assert "This session is closed." in page.text
    assert 'id="check-button"' not in page.text and 'id="run-button"' not in page.text
    assert "readonly" in page.text
    # No draft: the editor shows the last code sent, which the results are of.
    assert re.search(r'data-draft-key="cocco:exercise:[\d:]+"', page.text) is None
    assert 'hx-trigger="every 3s"' not in page.text
    assert _participants(db, session_id) == []
    response = client.post(f"{base}/1/check", data={"source": "print(42)"})
    assert response.status_code == 200
    assert "closed" in response.text
    assert _jobs(db) == []


async def test_the_landing_page_links_the_exercises_and_registers(client, world, db, session):
    session_id, base = session
    sign_in_as(client, STUDENT)
    page = client.get(base).text
    assert f'href="http://testserver{base}/1"' in page
    assert f"{base}/2" not in page
    assert _participants(db, session_id) == [world["bob"]]


async def test_a_revealed_solution_is_shown_under_the_statement(client, session):
    _, base = session
    client.post(f"{base}/reveal", data={"n": "1"})
    sign_in_as(client, STUDENT)
    page = client.get(f"{base}/1").text
    assert "Your teacher has shown the solution" in page
    assert "cocco-code" in page and "input" in page


async def test_staff_routes_beside_the_exercise_are_not_taken_for_one(client, session):
    """`{n:int}`: `.../dashboard` is not exercise "dashboard" refused as an
    unparseable number, it is a route of its own - and a segment that is
    neither is no route at all."""
    _, base = session
    assert client.get(f"{base}/dashboard").status_code == 200
    assert client.get(f"{base}/projector").status_code == 200
    assert client.get(f"{base}/elsewhere").status_code == 404


async def test_an_exercise_the_version_does_not_have_is_404(client, session):
    _, base = session
    sign_in_as(client, STUDENT)
    assert client.get(f"{base}/3").status_code == 404
    assert client.get(f"{base}/0").status_code == 404


########################################################################
# Waiting for the teacher


async def test_a_locked_exercise_is_a_page_to_wait_on(client, world, db, session):
    session_id, base = session
    course = world["course"]
    sign_in_as(client, STUDENT)
    page = client.get(f"{base}/2")
    assert page.status_code == 200
    assert "Wait for the teacher" in page.text
    assert "Shout" not in page.text  # not even its title
    assert "wait=1" in page.text
    assert _participants(db, session_id) == [world["bob"]]

    strip = f"/f/courses/{course}/sessions/{session_id}/strip?n=2&wait=1"
    still = client.get(strip)
    assert still.status_code == 200 and "HX-Redirect" not in still.headers

    sign_in_as(client, TEACHER)
    client.post(f"{base}/pace", data={"from": "1", "to": "2"})
    sign_in_as(client, STUDENT)
    opened = client.get(strip)
    assert opened.status_code == 204
    assert opened.headers["HX-Redirect"].endswith(f"{base}/2")


async def test_a_closed_session_has_nothing_to_wait_for(client, world, session):
    session_id, base = session
    client.post(f"{base}/close")
    sign_in_as(client, STUDENT)
    page = client.get(f"{base}/2")
    assert "closed before exercise 2" in page.text
    assert "wait=1" not in page.text
    strip = client.get(f"/f/courses/{world['course']}/sessions/{session_id}/strip?n=2&wait=1")
    assert strip.headers["HX-Redirect"].endswith(f"{base}/2")


async def test_a_poll_on_a_page_that_never_polls_does_not_redirect(client, world, session):
    session_id, _ = session
    sign_in_as(client, STUDENT)
    strip = client.get(f"/f/courses/{world['course']}/sessions/{session_id}/strip?n=1")
    assert strip.status_code == 200
    assert 'aria-current="page"' in strip.text
    assert "btn-disabled" in strip.text  # exercise 2, locked


########################################################################
# Run and Check


async def test_a_check_is_a_job_of_the_session_on_its_version(client, world, db, ready, session):
    _, version_id, _ = ready
    session_id, base = session
    sign_in_as(client, STUDENT)
    response = client.post(f"{base}/1/check", data={"source": "print(42)"})
    assert response.status_code == 200
    (job,) = _jobs(db)
    assert (job.kind, job.session_id, job.version_id, job.exercise_index) == (
        ExerciseJobKind.check, session_id, version_id, 1,
    )
    assert (job.user_id, job.source, job.stdin) == (world["bob"], "print(42)", None)
    assert f"/f/courses/{world['course']}/sessions/{session_id}/jobs/{job.id}" in response.text
    assert 'hx-trigger="every 500ms"' in response.text


async def test_a_run_carries_its_stdin(client, db, session):
    _, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/run", data={"source": "print(input())", "stdin": "7"})
    (job,) = _jobs(db)
    assert (job.kind, job.stdin) == (ExerciseJobKind.run, "7")


async def test_a_job_on_a_locked_exercise_is_refused_with_an_alert(client, db, session):
    _, base = session
    sign_in_as(client, STUDENT)
    response = client.post(f"{base}/2/check", data={"source": "print(1)"})
    assert response.status_code == 200
    assert "not unlocked yet" in response.text
    assert _jobs(db) == []


@pytest.mark.parametrize(
    "data, sentence",
    [
        ({"source": "#" * (65 << 10)}, "larger than 64 KB"),
        ({"source": "print(1)", "stdin": "a\x00b"}, "NUL character"),
    ],
)
async def test_too_much_or_a_nul_is_refused_with_an_alert(client, db, session, data, sentence):
    _, base = session
    sign_in_as(client, STUDENT)
    response = client.post(f"{base}/1/run", data=data)
    assert response.status_code == 200
    assert sentence in response.text
    assert _jobs(db) == []


async def test_the_page_opens_on_the_last_code_sent_and_the_last_results(client, db, session):
    _, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(41)"})
    client.post(f"{base}/1/run", data={"source": "print('newest')", "stdin": "99"})
    check, run = _jobs(db)
    _finish(db, check.id, SECRET_CHECK)
    page = client.get(f"{base}/1").text
    assert "print(&#39;newest&#39;)" in page
    assert ">99</textarea>" in page
    assert "1 of 2 tests passed." in page
    assert f"/jobs/{run.id}" in page  # the run, still polling


async def test_a_result_from_before_a_switch_says_so(client, world, db, ready, session):
    """Past results stay after a version switch - nothing is regraded - and
    say which version they ran on, on the page and in the fragment."""
    set_id, old_version, _ = ready
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    (check,) = _jobs(db)
    _finish(db, check.id, SECRET_CHECK)
    fragment = f"/f/courses/{world['course']}/sessions/{session_id}/jobs/{check.id}"
    assert "earlier version" not in client.get(f"{base}/1").text
    assert "earlier version" not in client.get(fragment).text

    sign_in_as(client, TEACHER)
    longer = _reimport(client, world, db, set_id, LONGER)
    client.post(f"{base}/switch", data={"version_id": str(longer)})
    sign_in_as(client, STUDENT)
    page = client.get(f"{base}/1").text
    assert "earlier version of the exercise" in page
    assert "1 of 2 tests passed." in page
    assert "earlier version of the exercise" in client.get(fragment).text


async def test_a_student_reads_a_hidden_test_as_its_verdict(client, world, db, session):
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    (job,) = _jobs(db)
    _finish(db, job.id, SECRET_CHECK)
    url = f"/f/courses/{world['course']}/sessions/{session_id}/jobs/{job.id}"
    fragment = client.get(url).text
    assert "every 500ms" not in fragment
    assert "1 of 2 tests passed." in fragment
    assert "SECRET" not in fragment and "Secret5" not in fragment
    assert "ValueError" in fragment
    assert "Invalid value" in fragment and "Line 3" not in fragment
    assert "1.23" not in fragment


async def test_a_job_is_its_askers_own(client, world, db, session):
    """Another student does not find it, and neither does staff: a polling
    tab never finds somebody else's job, and the dashboard is staff's door."""
    session_id, base = session
    with db.begin() as conn:
        carol = conn.execute(
            insert(User)
            .values(email="carol@unipd.it", nickname="carol", image=b"", image_mime="image/svg+xml")
            .returning(User.c.id)
        ).scalar_one()
        conn.execute(
            insert(Enrollment).values(user_id=carol, course_id=world["course"], role=UserRole.student)
        )
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/run", data={"source": "print(1)"})
    (job,) = _jobs(db)
    url = f"/f/courses/{world['course']}/sessions/{session_id}/jobs/{job.id}"
    assert client.get(url).status_code == 200
    sign_in_as(client, "carol@unipd.it")
    assert client.get(url).status_code == 404
    sign_in_as(client, TEACHER)
    assert client.get(url).status_code == 404


async def test_the_strip_follows_the_students_checks(client, world, db, session):
    """Checking, then solved - and solved stays solved after a wrong one."""
    session_id, base = session
    strip_url = f"/f/courses/{world['course']}/sessions/{session_id}/strip?n=1"
    sign_in_as(client, STUDENT)
    assert "not attempted" in client.get(strip_url).text
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    strip = client.get(strip_url).text
    assert "attempted, not solved yet, checking" in strip
    assert "loading-spinner" in strip

    (job,) = _jobs(db)
    _finish(db, job.id, ExerciseCheckResult(passed=True, tests=[]))
    client.post(f"{base}/1/check", data={"source": "print(0)"})
    second = _jobs(db)[-1]
    _finish(db, second.id, SECRET_CHECK)
    strip = client.get(strip_url).text
    assert "— solved" in strip and "btn-success" in strip
    assert "loading-spinner" not in strip


async def test_the_strip_is_the_askers_own(client, world, db, session):
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    sign_in_as(client, TEACHER)
    strip = client.get(f"/f/courses/{world['course']}/sessions/{session_id}/strip?n=1").text
    assert "checking" not in strip
    assert re.search(r"Double — not attempted", strip)


async def test_a_programs_output_is_escaped(client, world, db, session):
    """The fragment prints what a program wrote, and `.jinja` files are not
    autoescaped by default: it escapes for itself."""
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/run", data={"source": "print('<b>')"})
    (job,) = _jobs(db)
    _finish(db, job.id, ExerciseRunResult(status="ok", stdout="<script>alert(1)</script>\n"))
    url = f"/f/courses/{world['course']}/sessions/{session_id}/jobs/{job.id}"
    fragment = client.get(url).text
    assert "<script>" not in fragment
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment
    page = client.get(f"{base}/1").text
    assert "print(&#39;&lt;b&gt;&#39;)</textarea>" in page


def test_a_forged_line_is_no_line():
    """The report is the program's to forge, and its line is shown: a number
    no file has - one that encodes an input - is not passed on."""
    def line(value):
        return HarnessException.model_validate(
            {"type": "E", "mro": ["E"], "line": value, "traceback": ""}
        ).line

    assert line(int.from_bytes(b"secret input")) is None
    assert line(10**6 + 1) is None
    assert (line(0), line(-3), line(None)) == (None, None, None)
    assert (line(1), line(10**6)) == (1, 10**6)


async def test_a_check_after_the_teacher_went_back_is_refused(client, db, session):
    """The page was open on exercise 2; the teacher moved back to 1. The job
    gate reads the pacing as it is now."""
    _, base = session
    client.post(f"{base}/pace", data={"from": "1", "to": "2"})
    sign_in_as(client, STUDENT)
    assert client.get(f"{base}/2").status_code == 200
    sign_in_as(client, TEACHER)
    client.post(f"{base}/pace", data={"from": "2", "to": "1"})
    sign_in_as(client, STUDENT)
    response = client.post(f"{base}/2/check", data={"source": "print(1)"})
    assert response.status_code == 200
    assert "not unlocked yet" in response.text
    assert _jobs(db) == []


async def test_the_fallback_is_the_code_sent_last_not_the_job_made_last(client, db, session):
    """Run, then Check, then Run again while the first is still queued: the
    second Run rewrites the first in place - an older id - and its code is
    the one the page opens on."""
    _, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/run", data={"source": "print('first')"})
    client.post(f"{base}/1/check", data={"source": "print('check')"})
    client.post(f"{base}/1/run", data={"source": "print('last')"})
    run, check = _jobs(db)
    assert run.id < check.id and run.source == "print('last')"
    page = client.get(f"{base}/1").text
    assert "print(&#39;last&#39;)</textarea>" in page
