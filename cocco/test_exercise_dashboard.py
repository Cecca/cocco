"""The teacher's two screens of an exercise session: the dashboard, with
names and a drill-in per cell, and the projector, with neither - and the
functions that turn the progress document into what each is handed.

The set, the session and the people are `test_exercise_sessions.py`'s, and
jobs are finished by hand as in `test_exercise_student.py`. No judge runs.
"""

import re

import pytest
from sqlalchemy import insert

from cocco.conftest import sign_in_as
from cocco.model import Enrollment, ExerciseSessionParticipant, User, UserRole
from cocco.schema import ExerciseCheckResult, ExerciseRunResult
from cocco.web import exercise_sessions
from cocco.test_exercise_routes import (  # noqa: F401 - fixtures
    ADMIN,
    OTHER_TEACHER,
    STUDENT,
    TEACHER,
    client,
    ready,
    world,
)
from cocco.test_exercise_sessions import (  # noqa: F401 - fixtures
    LONGER,
    _reimport,
    session,
)
from cocco.test_exercise_student import SECRET_CHECK, _finish, _jobs, _user_id

CAROL = "carol@unipd.it"


@pytest.fixture
def carol(db, world) -> int:
    """A second student on the course."""
    with db.begin() as conn:
        carol = conn.execute(
            insert(User)
            .values(email=CAROL, nickname="carol", image=b"", image_mime="image/svg+xml")
            .returning(User.c.id)
        ).scalar_one()
        conn.execute(
            insert(Enrollment).values(user_id=carol, course_id=world["course"], role=UserRole.student)
        )
    return carol


def _progress(participants, titles=("Double", "Shout"), mode="live", unlocked_through=1):
    return {
        "id": 1,
        "label": "Lab A",
        "mode": mode,
        "unlocked_through": unlocked_through,
        "closed_at": None,
        "titles": list(titles),
        "revealed": [],
        "participants": participants,
    }


def _person(id_, nickname, *checks):
    return {
        "id": id_,
        "nickname": nickname,
        "email": f"{nickname}@unipd.it",
        "checks": [{"n": n, "solved": solved, "checking": checking} for n, solved, checking in checks],
    }


########################################################################
# The views


def test_the_progress_view_counts_every_status():
    view = exercise_sessions.session_progress_view(
        _progress(
            [
                _person(1, "bob", (1, True, False)),
                _person(2, "carol", (1, False, True), (2, False, False)),
                _person(3, "dan"),
            ]
        )
    )
    first, second = view["exercises"]
    assert (first["solved"], first["attempted"], first["not_attempted"]) == (1, 1, 1)
    assert first["checking"] == 1
    assert (second["solved"], second["attempted"], second["not_attempted"]) == (0, 1, 2)
    assert view["n_visible"] == 1
    carol = view["participants"][1]
    assert [cell["status"] for cell in carol["cells"]] == ["attempted", "attempted"]
    assert carol["cells"][0]["checking"]


def _counts(exercises, n_participants, titles=("Double", "Shout"), mode="live", unlocked_through=1):
    """`query_session_counts`'s document: per exercise checked, `(n,
    n_checked, n_solved, checking)`."""
    return {
        "id": 1,
        "set_id": 1,
        "version_id": 1,
        "label": "Lab A",
        "mode": mode,
        "unlocked_through": unlocked_through,
        "closed_at": None,
        "titles": list(titles),
        "revealed": [],
        "n_participants": n_participants,
        "exercises": [
            {"n": n, "n_checked": checked, "n_solved": solved, "checking": checking}
            for n, checked, solved, checking in exercises
        ],
    }


def test_the_projector_view_counts_every_status_up_to_the_pacing():
    view = exercise_sessions.projector_view(
        _counts(
            [(1, 2, 1, False), (2, 1, 0, True)],
            n_participants=3,
            titles=("Double", "Shout", "Secret title"),
        )
    )
    assert view["n_participants"] == 3
    (double,) = view["exercises"]
    assert (double["solved"], double["attempted"], double["not_attempted"]) == (1, 1, 1)
    assert view["n_locked"] == 2
    # A check pending on a locked exercise still keeps the bars polling.
    assert view["pending"]
    for leak in ("Secret title", "Shout"):
        assert leak not in repr(view)


def test_an_exercise_nobody_checked_is_not_attempted_by_all():
    view = exercise_sessions.projector_view(_counts([], n_participants=4, mode="open", unlocked_through=2))
    assert len(view["exercises"]) == 2 and view["n_locked"] == 0
    assert [e["not_attempted"] for e in view["exercises"]] == [4, 4]
    assert not view["pending"]


def test_the_progress_view_is_pending_while_any_check_is():
    assert exercise_sessions.session_progress_view(_progress([_person(1, "bob", (2, False, True))]))["pending"]
    assert not exercise_sessions.session_progress_view(_progress([_person(1, "bob", (1, True, False))]))[
        "pending"
    ]


########################################################################
# The dashboard


async def test_the_dashboard_is_staff_only(client, world, session):
    session_id, base = session
    assert client.get(f"{base}/dashboard").status_code == 200
    assert client.get(f"{base}/projector").status_code == 200
    fragments = [
        f"/f/courses/{world['course']}/sessions/{session_id}/dashboard",
        f"/f/courses/{world['course']}/sessions/{session_id}/projector",
    ]
    for url in fragments:
        assert client.get(url).status_code == 200
    sign_in_as(client, STUDENT)
    for url in [f"{base}/dashboard", f"{base}/projector", *fragments]:
        assert client.get(url).status_code == 403, url
    sign_in_as(client, OTHER_TEACHER)
    assert client.get(f"{base}/dashboard").status_code == 403
    sign_in_as(client, ADMIN)
    assert client.get(f"{base}/dashboard").status_code == 200


async def test_a_session_dashboard_is_reached_only_through_its_own_course(
    client, world, session
):
    session_id, _ = session
    assert client.get(f"/courses/{world['other']}/sessions/{session_id}/dashboard").status_code in (
        403,
        404,
    )


async def test_the_dashboard_names_the_cohort_and_not_the_staff(
    client, world, db, session, carol
):
    """Participants are who came: Bob opened the session, Carol never did, and
    the teacher looking at it is not one of them. A check without a
    participant row - a visit that raced a close - still counts."""
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.get(base)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    (check,) = _jobs(db)
    _finish(db, check.id, ExerciseCheckResult(passed=True, tests=[]))

    sign_in_as(client, TEACHER)
    page = client.get(f"{base}/dashboard").text
    assert "bob" in page and STUDENT in page
    assert "carol" not in page
    assert "alice" not in page.split('id="exercise-dashboard"')[1]
    assert re.search(r"\b1 participant\s", page)
    assert "bob, 1. Double — solved" in page
    assert "bob, 2. Shout — not attempted" in page

    with db.begin() as conn:
        conn.execute(
            ExerciseSessionParticipant.delete().where(
                ExerciseSessionParticipant.c.user_id == _user_id(db, STUDENT)
            )
        )
    grid = client.get(f"/f/courses/{world['course']}/sessions/{session_id}/dashboard").text
    assert "bob, 1. Double — solved" in grid


async def test_a_cell_follows_the_checks(client, world, db, session):
    """Checking, then attempted - and a pass stays a pass, as on the strip."""
    session_id, base = session
    grid_url = f"/f/courses/{world['course']}/sessions/{session_id}/dashboard"
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(0)"})
    sign_in_as(client, TEACHER)
    assert "bob, 1. Double — attempted, not solved yet, checking…" in client.get(grid_url).text

    first = _jobs(db)[-1]
    _finish(db, first.id, ExerciseCheckResult(passed=True, tests=[]))
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(0)"})
    _finish(db, _jobs(db)[-1].id, SECRET_CHECK)
    sign_in_as(client, TEACHER)
    grid = client.get(grid_url).text
    assert "bob, 1. Double — solved" in grid
    assert "checking" not in grid


async def test_a_student_name_is_escaped(client, world, db, session):
    _, base = session
    with db.begin() as conn:
        conn.execute(
            User.update().where(User.c.email == STUDENT).values(nickname="<b>bob</b>")
        )
    sign_in_as(client, STUDENT)
    client.get(base)
    sign_in_as(client, TEACHER)
    page = client.get(f"{base}/dashboard").text
    assert "<b>bob</b>" not in page
    assert "&lt;b&gt;bob&lt;/b&gt;" in page


########################################################################
# The drill-in


def _drill_in(world, session_id, user_id, n=1):
    return f"/f/courses/{world['course']}/sessions/{session_id}/dashboard/{user_id}/{n}"


async def test_the_drill_in_shows_the_code_and_every_test_in_full(client, world, db, session):
    """Staff read a student's hidden test in full - the opposite of what the
    student is shown of it."""
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print('my check')"})
    client.post(f"{base}/1/run", data={"source": "print('my run')", "stdin": "seven"})
    check, run = _jobs(db)
    sign_in_as(client, TEACHER)
    url = _drill_in(world, session_id, _user_id(db, STUDENT))
    pending = client.get(url).text
    assert "every 2s" in pending  # it polls while a job is pending

    _finish(db, check.id, SECRET_CHECK)
    _finish(db, run.id, ExerciseRunResult(status="ok", stdout="RUN-OUTPUT\n"))
    page = client.get(url).text
    assert "every 2s" not in page and "every 500ms" not in page
    assert "my check" in page and "my run" in page and "seven" in page
    assert "Latest Check" in page and "Latest Run" in page
    assert "the input was 5-SECRET" in page
    assert "RUN-OUTPUT" in page
    assert "1 of 2 tests passed." in page


async def test_the_drill_in_of_a_cell_never_tried(client, world, db, session):
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.get(base)
    sign_in_as(client, TEACHER)
    page = client.get(_drill_in(world, session_id, _user_id(db, STUDENT), n=2)).text
    assert "No Run and no Check of this exercise yet." in page


async def test_the_drill_in_names_only_the_sessions_own_people(
    client, world, db, session, carol
):
    """Carol is on the course but never came; the teacher is nobody's cohort;
    an exercise the version does not have is no exercise."""
    session_id, base = session
    assert client.get(_drill_in(world, session_id, carol)).status_code == 404
    assert client.get(_drill_in(world, session_id, _user_id(db, TEACHER))).status_code == 404
    sign_in_as(client, STUDENT)
    client.get(base)
    sign_in_as(client, TEACHER)
    bob = _user_id(db, STUDENT)
    assert client.get(_drill_in(world, session_id, bob)).status_code == 200
    assert client.get(_drill_in(world, session_id, bob, n=3)).status_code == 404
    sign_in_as(client, STUDENT)
    assert client.get(_drill_in(world, session_id, bob)).status_code == 403


async def test_the_drill_in_reads_a_check_against_the_version_it_ran_on(
    client, world, db, ready, session
):
    set_id, _, _ = ready
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    (check,) = _jobs(db)
    _finish(db, check.id, SECRET_CHECK)
    sign_in_as(client, TEACHER)
    url = _drill_in(world, session_id, _user_id(db, STUDENT))
    assert "earlier version" not in client.get(url).text

    longer = _reimport(client, world, db, set_id, LONGER)
    client.post(f"{base}/switch", data={"version_id": str(longer)})
    page = client.get(url).text
    assert "earlier version of the exercise than the session is on now" in page
    # The old version's hidden input, which the check ran on.
    assert "1 of 2 tests passed." in page


########################################################################
# The projector


async def test_the_projector_shows_no_names(client, world, db, session, carol):
    _, base = session
    for email in (STUDENT, CAROL):
        sign_in_as(client, email)
        client.get(base)
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    _finish(db, _jobs(db)[-1].id, ExerciseCheckResult(passed=True, tests=[]))

    sign_in_as(client, TEACHER)
    page = client.get(f"{base}/projector").text
    for leak in ("bob", "carol", STUDENT, CAROL):
        assert leak not in page
    assert re.search(r">2</span>\s+participants", page)
    assert "1 solved, 0 tried, 1 not tried" in page
    assert "Double" in page and "Shout" not in page
    assert "1 more exercise still locked." in page


async def test_the_projector_opens_the_editor_in_the_staff_preview(client, world, ready, session):
    """A teacher writing a solution in front of the class does it in the
    preview of the session's version: the session's own page refuses staff's
    Run and Check."""
    set_id, version_id, _ = ready
    _, base = session
    page = client.get(f"{base}/projector").text
    preview = f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/preview/1"
    assert preview in page
    assert client.get(preview).status_code == 200


async def test_pacing_from_the_projector_answers_with_the_projector(client, world, db, session):
    _, base = session
    response = client.post(f"{base}/pace", data={"from": "1", "to": "2", "view": "projector"})
    assert response.status_code == 200
    assert 'id="exercise-projector"' in response.text
    assert 'id="exercise-session-controls"' not in response.text
    assert "Shout" in response.text
    stale = client.post(f"{base}/pace", data={"from": "1", "to": "2", "view": "projector"})
    assert 'id="exercise-projector"' in stale.text
    assert "Nothing was changed." in stale.text
    # The panel's own answer is unchanged.
    panel = client.post(f"{base}/pace", data={"from": "2", "to": "1"})
    assert 'id="exercise-session-controls"' in panel.text


async def test_a_solution_is_projected_only_once_revealed(client, world, db, session):
    _, base = session
    assert "print(2 * n)" not in client.get(f"{base}/projector?solution=1").text
    response = client.post(f"{base}/reveal", data={"n": "1", "view": "projector"})
    assert 'id="exercise-projector"' in response.text
    assert "Show solution" in response.text
    page = client.get(f"{base}/projector?solution=1").text
    assert "Solution · 1. Double" in page
    assert "2" in page and "print" in page and "Hide" in page
    assert "Solution ·" not in client.get(f"{base}/projector?solution=2").text


async def test_a_closed_session_is_polled_until_its_last_check_lands(client, world, db, session):
    """Closing refuses new jobs and cancels none: the grid, the projector and
    the strip keep polling while a check from before the close is pending,
    and stop once it is done."""
    session_id, base = session
    sign_in_as(client, STUDENT)
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    sign_in_as(client, TEACHER)
    client.post(f"{base}/close")
    course = world["course"]
    grid = f"/f/courses/{course}/sessions/{session_id}/dashboard"
    bars = f"/f/courses/{course}/sessions/{session_id}/projector"
    strip = f"/f/courses/{course}/sessions/{session_id}/strip?n=1"
    assert "every 3s" in client.get(grid).text
    assert "every 3s" in client.get(bars).text
    sign_in_as(client, STUDENT)
    assert "every 3s" in client.get(strip).text

    _finish(db, _jobs(db)[-1].id, ExerciseCheckResult(passed=True, tests=[]))
    assert "every 3s" not in client.get(strip).text
    sign_in_as(client, TEACHER)
    assert "every 3s" not in client.get(grid).text
    assert "every 3s" not in client.get(bars).text
    assert "bob, 1. Double — solved" in client.get(grid).text


async def test_the_projector_forms_share_the_polls_queue_and_guard_themselves(client, session):
    """`hx-sync` so a stale poll cannot land after a press, and a guard of
    their own so a refused press does not freeze the polling projector."""
    _, base = session
    page = client.get(f"{base}/projector").text
    fragment = page.split('id="exercise-projector"', 1)[1]
    forms = re.findall(r"<form\b[^>]*>", fragment)
    assert len(forms) == 4  # three pacing buttons and a reveal (one exercise visible)
    for form in forms:
        assert 'hx-sync="closest #exercise-projector:replace"' in form
        assert "data-swap-guard" in form
