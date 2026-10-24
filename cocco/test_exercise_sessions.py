"""End-to-end tests of exercise sessions: opening one on a set, pacing it,
revealing solutions, closing it and moving it to a newer version - and the
gate every student-facing exercise route will go through.

The set is imported and built as in `test_exercise_routes.py`, whose helpers
these are. No judge runs.
"""

import asyncio
import re
import textwrap

import pytest
from fastapi import HTTPException
from sqlalchemy import func, insert, select, update

from cocco.repository import judging as repo
from cocco.exercises import format as exercise_format
from cocco.conftest import sign_in_as
from cocco.repository.exercise_sessions import (
    close_exercise_session,
    insert_exercise_session,
    lock_exercise_set_for_session,
    query_exercise_session,
    query_exercise_session_state,
    reveal_exercise_solution,
    switch_exercise_session_version,
)
from cocco.repository.exercise_sets import lock_exercise_set
from cocco.repository.lifecycle import delete_exercise_set
from cocco.web import exercise_sessions
from cocco.web.access import Forbidden
from cocco.model import (
    Course,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSessionMode,
    ExerciseSessionParticipant,
    ExerciseSessionReveal,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
    UserRole,
)
from cocco.test_exercise_routes import (  # noqa: F401 - fixtures
    ADMIN,
    OTHER_TEACHER,
    SOURCE,
    STUDENT,
    TEACHER,
    _build,
    _count,
    _import,
    _imported,
    _versions,
    client,
    ready,
    world,
)

# Three exercises: SOURCE's two, then one appended.
LONGER = SOURCE + textwrap.dedent(
    """
    # Third

    Print three.

    ```solution
    print(3)
    ```
    """
)

# SOURCE's two, the other way round: what position-matching cannot catch.
SWAPPED = SOURCE.replace("# Double", "# TMP").replace("# Shout", "# Double").replace(
    "# TMP", "# Shout"
)

# SOURCE's first exercise only.
SHORTER = SOURCE.split("# Shout")[0]


def _open(client, world, set_id, label="Lab A", mode="live"):
    return client.post(
        f"/courses/{world['course']}/exercises/{set_id}/sessions",
        data={"label": label, "mode": mode},
    )


def _opened(client, world, set_id, **kwargs) -> int:
    response = _open(client, world, set_id, **kwargs)
    assert response.status_code == 204, response.text
    return int(re.search(r"/sessions/(\d+)$", response.headers["HX-Redirect"])[1])


def _session(db, session_id):
    with db.connect() as conn:
        return conn.execute(
            select(ExerciseSession).where(ExerciseSession.c.id == session_id)
        ).one()


def _reimport(client, world, db, set_id, source) -> int:
    """Import `source` into the set and build it: answers the new version."""
    response = _import(client, world["course"], source, set_id=set_id)
    assert response.status_code == 200, response.text
    version_id = _versions(db, set_id)[-1].id
    _build(db, version_id, source)
    return version_id


def _check(db, session_id, version_id, n, user_id) -> None:
    with db.begin() as conn:
        conn.execute(
            insert(ExerciseJob).values(
                kind=ExerciseJobKind.check,
                user_id=user_id,
                version_id=version_id,
                session_id=session_id,
                exercise_index=n,
                source="print(1)",
            )
        )


@pytest.fixture
def session(client, world, ready):
    """A live session on the ready set: (session_id, base url)."""
    set_id, _, _ = ready
    session_id = _opened(client, world, set_id)
    return session_id, f"/courses/{world['course']}/sessions/{session_id}"


########################################################################
# Opening


async def test_opening_a_live_session_pins_the_current_version(client, world, db, ready):
    set_id, version_id, _ = ready
    session_id = _opened(client, world, set_id, label="  Lab A — 20 Oct ")
    row = _session(db, session_id)
    assert (row.set_id, row.version_id, row.label) == (set_id, version_id, "Lab A — 20 Oct")
    assert row.mode == ExerciseSessionMode.live
    assert row.unlocked_through == 1
    assert row.closed_at is None


async def test_an_open_session_is_stored_fully_unlocked(client, world, db, ready):
    set_id, _, _ = ready
    row = _session(db, _opened(client, world, set_id, mode="open"))
    assert row.mode == ExerciseSessionMode.open
    assert row.unlocked_through == 2


@pytest.mark.parametrize(
    "label, mode, sentence",
    [
        ("", "live", "Give the session a label"),
        ("x" * 201, "live", "longer than 200"),
        ("Lab A", "graded", "live or open"),
    ],
)
async def test_a_refused_opening_creates_nothing(client, world, db, ready, label, mode, sentence):
    set_id, _, _ = ready
    response = _open(client, world, set_id, label=label, mode=mode)
    assert response.status_code == 200
    assert sentence in response.text
    assert _count(db, ExerciseSession) == 0


async def test_no_session_on_a_set_with_no_ready_version(client, world, db):
    set_id = _imported(client, world)  # still building
    response = _open(client, world, set_id)
    assert response.status_code == 200
    assert "nothing to open" in response.text
    assert _count(db, ExerciseSession) == 0
    assert "disabled" in client.get(f"/courses/{world['course']}/exercises/{set_id}").text


async def test_the_set_page_lists_its_sessions(client, world, session, ready):
    set_id, _, _ = ready
    page = client.get(f"/courses/{world['course']}/exercises/{set_id}")
    assert "Lab A" in page.text
    assert session[1] in page.text


async def test_opening_a_session_is_staff_only(client, world, db, ready):
    set_id, _, _ = ready
    sign_in_as(client, STUDENT)
    assert _open(client, world, set_id).status_code == 403
    sign_in_as(client, OTHER_TEACHER)
    assert _open(client, world, set_id).status_code == 403
    # Dave naming his own course in front of Alice's set.
    response = client.post(
        f"/courses/{world['other']}/exercises/{set_id}/sessions",
        data={"label": "x", "mode": "live"},
    )
    assert response.status_code == 404
    assert _count(db, ExerciseSession) == 0


########################################################################
# Who sees what


async def test_staff_get_the_control_panel(client, session):
    page = client.get(session[1])
    assert page.status_code == 200
    assert 'id="exercise-session-controls"' in page.text
    assert "1</span> of 2 unlocked" in page.text
    assert "Reveal solution" in page.text


async def test_a_student_sees_only_what_is_unlocked(client, session):
    sign_in_as(client, STUDENT)
    page = client.get(session[1])
    assert page.status_code == 200
    assert "exercise-session-controls" not in page.text
    assert "Warm-up" in page.text
    assert "Double" in page.text
    assert "Shout" not in page.text
    assert "1 more exercise still locked" in page.text


async def test_a_student_cannot_pace_reveal_or_close(client, db, session):
    session_id, base = session
    sign_in_as(client, STUDENT)
    for path, data in (
        ("pace", {"from": "1", "to": "2"}),
        ("open", {}),
        ("close", {}),
        ("reveal", {"n": "1"}),
        ("switch", {"version_id": "1"}),
    ):
        assert client.post(f"{base}/{path}", data=data).status_code == 403, path
    row = _session(db, session_id)
    assert (row.unlocked_through, row.mode, row.closed_at) == (1, ExerciseSessionMode.live, None)


async def test_a_session_is_reached_only_through_its_own_course(client, world, session):
    """A stranger is refused by the membership gate; staff of another course
    naming their course in front of this session find nothing."""
    session_id, base = session
    sign_in_as(client, OTHER_TEACHER)
    assert client.get(base).status_code == 403
    other = f"/courses/{world['other']}/sessions/{session_id}"
    assert client.get(other).status_code == 404
    assert client.post(f"{other}/pace", data={"from": "1", "to": "2"}).status_code == 404
    assert client.get(f"/courses/{world['course']}/sessions/999").status_code == 403
    sign_in_as(client, STUDENT)
    assert client.get(f"/courses/{world['course']}/sessions/999").status_code == 404


async def test_an_administrator_gets_the_control_panel(client, session):
    sign_in_as(client, ADMIN)
    assert 'id="exercise-session-controls"' in client.get(session[1]).text


async def test_the_course_page_lists_open_sessions_and_folds_closed_ones(
    client, world, ready, session
):
    set_id, _, _ = ready
    closed = _opened(client, world, set_id, label="Lab Z")
    client.post(f"/courses/{world['course']}/sessions/{closed}/close")
    sign_in_as(client, STUDENT)
    page = client.get(f"/courses/{world['course']}").text
    assert "Exercise sessions" in page
    head, folded = page.split("Closed sessions (1)")
    assert "Lab A" in head and "Lab Z" not in head
    assert "Lab Z" in folded


async def test_a_non_member_is_not_shown_the_sessions(client, world, session):
    sign_in_as(client, OTHER_TEACHER)
    page = client.get(f"/courses/{world['course']}").text
    assert "Exercise sessions" not in page
    assert "Lab A" not in page


########################################################################
# Pacing


async def test_next_previous_and_unlock_all(client, db, session):
    session_id, base = session
    assert client.post(f"{base}/pace", data={"from": "1", "to": "2"}).status_code == 200
    assert _session(db, session_id).unlocked_through == 2
    client.post(f"{base}/pace", data={"from": "2", "to": "1"})
    assert _session(db, session_id).unlocked_through == 1
    client.post(f"{base}/pace", data={"from": "1", "to": "0"})
    assert _session(db, session_id).unlocked_through == 0
    client.post(f"{base}/pace", data={"from": "0", "to": "2"})
    assert _session(db, session_id).unlocked_through == 2


async def test_pacing_is_clamped_to_the_version(client, db, session):
    session_id, base = session
    client.post(f"{base}/pace", data={"from": "1", "to": "9"})
    assert _session(db, session_id).unlocked_through == 2
    client.post(f"{base}/pace", data={"from": "2", "to": "-3"})
    assert _session(db, session_id).unlocked_through == 0


async def test_two_teachers_pressing_next_unlock_one_exercise(client, db, session):
    """Both pages showed 1; the second press finds the first already moved it
    and changes nothing - it neither skips an exercise nor takes the class
    back."""
    session_id, base = session
    client.post(f"{base}/pace", data={"from": "1", "to": "2"})
    response = client.post(f"{base}/pace", data={"from": "1", "to": "2"})
    assert response.status_code == 200
    assert "changed since this page was loaded" in response.text
    assert _session(db, session_id).unlocked_through == 2
    # A stale "previous" does not undo it either.
    client.post(f"{base}/pace", data={"from": "1", "to": "0"})
    assert _session(db, session_id).unlocked_through == 2


async def test_switching_to_open_ends_the_pacing(client, db, session):
    session_id, base = session
    response = client.post(f"{base}/open")
    assert "every exercise is unlocked" in response.text
    row = _session(db, session_id)
    # Stored fully unlocked, as an open session opened as one is.
    assert (row.mode, row.unlocked_through) == (ExerciseSessionMode.open, 2)
    # Pacing an open session changes nothing, and says why.
    response = client.post(f"{base}/pace", data={"from": "2", "to": "1"})
    assert "every exercise is already unlocked" in response.text
    assert _session(db, session_id).unlocked_through == 2
    assert "Nothing was changed" in client.post(f"{base}/open").text


async def test_a_refused_pacing_says_what_stopped_it(client, db, session):
    _, base = session
    assert "did not say where the session stood" in client.post(
        f"{base}/pace", data={"to": "2"}
    ).text
    assert "changed since this page was loaded" in client.post(
        f"{base}/pace", data={"from": "2", "to": "1"}
    ).text
    client.post(f"{base}/close")
    assert "not paced any more" in client.post(
        f"{base}/pace", data={"from": "1", "to": "2"}
    ).text


async def test_a_closed_session_is_not_paced_or_opened(client, db, session):
    session_id, base = session
    response = client.post(f"{base}/close")
    assert "The session is closed." in response.text
    closed_at = _session(db, session_id).closed_at
    assert closed_at is not None
    client.post(f"{base}/pace", data={"from": "1", "to": "2"})
    client.post(f"{base}/open")
    row = _session(db, session_id)
    assert (row.unlocked_through, row.mode) == (1, ExerciseSessionMode.live)
    assert "already closed" in client.post(f"{base}/close").text
    assert _session(db, session_id).closed_at == closed_at
    assert "Close session" not in client.get(base).text


########################################################################
# Revealing


async def test_revealing_a_solution(client, db, session):
    session_id, base = session
    response = client.post(f"{base}/reveal", data={"n": "2"})
    assert response.status_code == 200
    assert "solution revealed" in response.text
    client.post(f"{base}/reveal", data={"n": "2"})  # twice is a no-op
    with db.connect() as conn:
        rows = conn.execute(select(ExerciseSessionReveal.c.exercise_index)).scalars().all()
    assert rows == [2]
    assert client.post(f"{base}/reveal", data={"n": "3"}).status_code == 404
    assert client.post(f"{base}/reveal", data={"n": "0"}).status_code == 404


async def test_a_closed_session_can_still_reveal(client, db, session):
    _, base = session
    client.post(f"{base}/close")
    assert "solution revealed" in client.post(f"{base}/reveal", data={"n": "1"}).text


########################################################################
# Switching to the latest version


async def test_a_session_on_the_current_version_offers_no_switch(client, session):
    page = client.get(session[1]).text
    assert "the set's current version" in page
    assert "switch_version_modal" not in page


async def test_switching_lists_renamed_and_appended_exercises(client, world, db, ready, session):
    """Exercises are matched by position: a swapped file renames both, and
    the confirmation says so before anything moves."""
    set_id, old_version, _ = ready
    session_id, base = session
    swapped = _reimport(client, world, db, set_id, SWAPPED + LONGER.split(SOURCE)[1])
    page = client.get(base).text
    assert "Switch to v2" in page
    assert "Titles that changed" in page
    assert re.search(r"1\. <span[^>]*>Double</span> → Shout", page)
    assert "Added at the end" in page and "3. Third" in page
    assert _session(db, session_id).version_id == old_version

    response = client.post(f"{base}/switch", data={"version_id": str(swapped)})
    assert response.status_code == 200
    assert "Switched to the latest version." in response.text
    assert _session(db, session_id).version_id == swapped


async def test_a_switch_keeps_past_checks_and_clamps_the_pacing(client, world, db, ready, session):
    set_id, old_version, _ = ready
    session_id, base = session
    client.post(f"{base}/pace", data={"from": "1", "to": "2"})
    client.post(f"{base}/reveal", data={"n": "2"})
    _check(db, session_id, old_version, 1, world["bob"])
    shorter = _reimport(client, world, db, set_id, SHORTER)
    page = client.get(base).text
    assert "No longer in the file" in page and "2. Shout" in page

    client.post(f"{base}/switch", data={"version_id": str(shorter)})
    row = _session(db, session_id)
    assert (row.version_id, row.unlocked_through) == (shorter, 1)
    with db.connect() as conn:
        assert conn.execute(select(ExerciseSessionReveal)).all() == []
        (check,) = conn.execute(select(ExerciseJob).where(ExerciseJob.c.session_id == session_id)).all()
    assert check.version_id == old_version


async def test_a_switch_is_refused_below_the_checked_exercises(client, world, db, ready, session):
    """A check on exercise 2 needs an exercise 2: the refusal names it, on the
    page and from the route."""
    set_id, old_version, _ = ready
    session_id, base = session
    _check(db, session_id, old_version, 2, world["bob"])
    shorter = _reimport(client, world, db, set_id, SHORTER)
    page = client.get(base).text
    assert "checks on exercise 2, “Shout”" in page
    assert "switch_version_modal" not in page

    response = client.post(f"{base}/switch", data={"version_id": str(shorter)})
    assert "checks on exercise 2, “Shout”" in response.text
    assert "Nothing was changed" in response.text
    assert _session(db, session_id).version_id == old_version


async def test_a_switch_to_a_version_nobody_confirmed_is_refused(client, world, db, ready, session):
    """The confirmation listed v2; v3 became current since. Switching to it
    unseen would skip the one check position-matching needs."""
    set_id, old_version, _ = ready
    session_id, base = session
    v2 = _reimport(client, world, db, set_id, LONGER)
    _reimport(client, world, db, set_id, SWAPPED)
    response = client.post(f"{base}/switch", data={"version_id": str(v2)})
    assert "A newer version became current" in response.text
    assert _session(db, session_id).version_id == old_version


C_VERSION = """\
---
title: Warm-up
language: c
---

# One

Return.

```c solution
int main(void) { return 0; }
```
"""


async def test_a_switch_across_languages_is_refused(client, world, db, ready, session):
    """A session's language is fixed: the page offers no switch, and the route
    answers 200 with the alert in the controls fragment."""
    set_id, old_version, _ = ready
    session_id, base = session
    c_version = _reimport(client, world, db, set_id, C_VERSION)
    page = client.get(base).text
    assert "is written in c and this session is on python" in page
    assert "switch_version_modal" not in page

    response = client.post(f"{base}/switch", data={"version_id": str(c_version)})
    assert response.status_code == 200
    assert "is written in c and this session is on python" in response.text
    assert "Nothing was changed" in response.text
    assert _session(db, session_id).version_id == old_version


async def test_a_closed_session_keeps_its_version(client, world, db, ready, session):
    set_id, old_version, _ = ready
    session_id, base = session
    v2 = _reimport(client, world, db, set_id, LONGER)
    client.post(f"{base}/close")
    response = client.post(f"{base}/switch", data={"version_id": str(v2)})
    assert "its version is final" in response.text
    assert _session(db, session_id).version_id == old_version


########################################################################
# The gate on an exercise of a session


async def test_the_read_gate_follows_the_pacing(client, db, app_engine, session):
    """Live: 1..unlocked_through for a student - a locked exercise is a
    Forbidden page with a sentence - and everything for staff. An exercise the
    version does not have is a 404 for everybody."""
    session_id, base = session
    async with app_engine.connect() as conn:
        allowed = await exercise_sessions.require_session_exercise(session_id, 1, conn, UserRole.student)
        assert allowed.n == 1 and allowed.state.unlocked_through == 1
        with pytest.raises(Forbidden, match="Exercise 2 is not unlocked yet"):
            await exercise_sessions.require_session_exercise(session_id, 2, conn, UserRole.student)
        await exercise_sessions.require_session_exercise(session_id, 2, conn, UserRole.teacher)
        for n in (0, 3):
            with pytest.raises(HTTPException) as refused:
                await exercise_sessions.require_session_exercise(session_id, n, conn, UserRole.teacher)
            assert refused.value.status_code == 404
        with pytest.raises(HTTPException) as refused:
            await exercise_sessions.require_session_exercise(999, 1, conn, UserRole.teacher)
        assert refused.value.status_code == 404

    client.post(f"{base}/open")
    async with app_engine.connect() as conn:
        await exercise_sessions.require_session_exercise(session_id, 2, conn, UserRole.student)


async def test_a_closed_session_is_read_but_takes_no_jobs(client, db, app_engine, session):
    session_id, base = session
    client.post(f"{base}/close")
    async with app_engine.connect() as conn:
        await exercise_sessions.require_session_exercise(session_id, 1, conn, UserRole.student)
        for role in (UserRole.student, UserRole.teacher):
            refusal = await exercise_sessions.session_job_refusal(conn, session_id, 1, role)
            assert refusal is not None and "closed" in refusal
        await conn.rollback()


async def test_the_job_gate_follows_the_pacing(client, db, app_engine, session):
    session_id, base = session
    async with app_engine.connect() as conn:
        assert await exercise_sessions.session_job_refusal(conn, session_id, 1, UserRole.student) is None
        refusal = await exercise_sessions.session_job_refusal(conn, session_id, 2, UserRole.student)
        assert "not unlocked yet" in refusal
        assert await exercise_sessions.session_job_refusal(conn, session_id, 2, UserRole.teacher) is None
        with pytest.raises(HTTPException):
            await exercise_sessions.session_job_refusal(conn, session_id, 3, UserRole.student)
        await conn.rollback()


async def test_the_job_gate_holds_the_session_until_the_job_commits(
    client, db, app_engine, session
):
    """`FOR SHARE`: a close - an `UPDATE` of the row - waits for the
    transaction that queued a job to end, so the job never lands in a session
    that closed after it was let in."""
    session_id, _ = session
    async with app_engine.connect() as gate, app_engine.connect() as closer:
        assert await exercise_sessions.session_job_refusal(gate, session_id, 1, UserRole.student) is None
        closing = asyncio.create_task(close_exercise_session(closer, session_id))
        await asyncio.sleep(0.2)
        assert not closing.done()
        await gate.commit()
        assert await closing
        await closer.commit()


async def test_the_student_view_carries_only_what_it_shows(client, db, app_engine, session):
    session_id, base = session
    client.post(f"{base}/reveal", data={"n": "2"})
    async with app_engine.connect() as conn:
        document = await query_exercise_session(conn, session_id)
    view = exercise_sessions.student_session_view(document)
    assert set(view) == {"label", "mode", "closed_at", "title", "titles", "n_locked"}
    assert (view["titles"], view["n_locked"]) == (["Double"], 1)


async def test_the_crumb_names_the_set_as_it_is_now(client, world, db, ready, session):
    """The session stays on v1, Warm-up; the set is called what its current
    version is called."""
    set_id, _, _ = ready
    _reimport(client, world, db, set_id, SOURCE.replace("title: Warm-up", "title: Renamed"))
    page = client.get(session[1]).text
    crumbs = page.split("<main")[0]
    assert "Renamed" in crumbs and "Warm-up" not in crumbs
    assert "of Warm-up" in page  # the version the session is on


async def test_switching_an_open_session_keeps_it_fully_unlocked(client, world, db, ready):
    set_id, _, _ = ready
    session_id = _opened(client, world, set_id, mode="open")
    longer = _reimport(client, world, db, set_id, LONGER)
    client.post(
        f"/courses/{world['course']}/sessions/{session_id}/switch",
        data={"version_id": str(longer)},
    )
    row = _session(db, session_id)
    assert (row.version_id, row.unlocked_through) == (longer, 3)


async def test_the_open_button_follows_the_polled_status(client, world, db):
    """Drawn disabled while nothing has built, in place on the page; every
    poll carries a fresh one out of band, enabled once a version is ready."""
    set_id = _imported(client, world)
    url = f"/courses/{world['course']}/exercises/{set_id}"
    page = client.get(url).text
    button = page.split('id="exercise-session-open-button"')[1].split("</span>")[0]
    assert "hx-swap-oob" not in page.split('id="exercise-session-open-button"')[0][-200:]
    assert "disabled" in button
    _build(db, _versions(db, set_id)[0].id)
    fragment = client.get(f"/f{url}").text
    assert 'id="exercise-session-open-button"' in fragment
    oob = fragment.split('id="exercise-session-open-button"')[1].split("</button>")[0]
    assert 'hx-swap-oob="true"' in oob
    assert "disabled" not in oob


########################################################################
# Races. Each holds one side's transaction open on its own connection and
# shows the other side waiting for it, then reading what it left.


async def _waits(task) -> None:
    await asyncio.sleep(0.2)
    assert not task.done()


async def test_a_job_gate_behind_a_switch_reads_the_new_version(
    client, world, db, app_engine, ready, session
):
    """The gate waits for the switch's row lock and then answers on the
    version the session is now on - not a 404 for a session the locking read
    could no longer join to its old version."""
    set_id, _, _ = ready
    session_id, _ = session
    longer = _reimport(client, world, db, set_id, LONGER)
    async with app_engine.connect() as switcher, app_engine.connect() as gate:
        await query_exercise_session_state(switcher, session_id, lock="update")
        await switch_exercise_session_version(switcher, session_id, longer, 3)
        task = asyncio.create_task(
            exercise_sessions.session_job_refusal(gate, session_id, 1, UserRole.student)
        )
        await _waits(task)
        await switcher.commit()
        assert await task is None
        state = await query_exercise_session_state(gate, session_id)
        assert (state.version_id, state.n_exercises) == (longer, 3)
        await gate.rollback()


async def test_a_reveal_behind_a_switch_is_checked_against_the_new_version(
    client, world, db, app_engine, ready, session
):
    set_id, _, _ = ready
    session_id, _ = session
    shorter = _reimport(client, world, db, set_id, SHORTER)
    async with app_engine.connect() as switcher, app_engine.connect() as revealer:
        await query_exercise_session_state(switcher, session_id, lock="update")
        await switch_exercise_session_version(switcher, session_id, shorter, 1)
        task = asyncio.create_task(reveal_exercise_solution(revealer, session_id, 2))
        await _waits(task)
        await switcher.commit()
        assert await task is False
        await revealer.commit()
    assert _count(db, ExerciseSessionReveal) == 0


async def test_an_opening_behind_the_first_build_opens_on_it(client, world, db, app_engine):
    """The build moves `current_version_id` while the opening waits for the
    set: the opening reads the version it left, and its count."""
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id
    document = exercise_format.parse(SOURCE).document
    with db.connect() as builder:
        builder.begin()
        repo.finish_version_build(
            builder, version_id, ExerciseVersionStatus.ready, [],
            document.model_dump(mode="json"),
        )
        async with app_engine.connect() as opener:
            task = asyncio.create_task(lock_exercise_set_for_session(opener, set_id))
            await _waits(task)
            builder.commit()
            found = await task
            await opener.rollback()
    assert (found.current_version_id, found.n_exercises) == (version_id, 2)


async def test_an_opening_and_a_set_delete_serialize(client, world, db, app_engine):
    """Whichever locks the set first wins: a delete that got there first
    leaves the opening nothing to open on, and an opening that got there first
    is counted by the delete."""
    set_id = _imported(client, world)
    version_id = _versions(db, set_id)[0].id
    _build(db, version_id)
    async with app_engine.connect() as opener, app_engine.connect() as deleter:
        # The opening first: the delete waits, then counts its session.
        found = await lock_exercise_set_for_session(opener, set_id)
        task = asyncio.create_task(lock_exercise_set(deleter, set_id))
        await _waits(task)
        await insert_exercise_session(
            opener, set_id, found.current_version_id, "Lab A",
            ExerciseSessionMode.live, 1,
        )
        await opener.commit()
        assert await task == 1
        await deleter.rollback()

    second = _imported(client, world)
    _build(db, _versions(db, second)[0].id)
    async with app_engine.connect() as opener, app_engine.connect() as deleter:
        # The delete first: the opening waits, then finds no set.
        assert await lock_exercise_set(deleter, second) == 0
        task = asyncio.create_task(lock_exercise_set_for_session(opener, second))
        await _waits(task)
        await delete_exercise_set(deleter, second)
        await deleter.commit()
        assert await task is None
        await opener.rollback()


########################################################################
# Deletion and archiving


def _rows(db, table, *where) -> list:
    with db.connect() as conn:
        return conn.execute(select(table).where(*where)).all()


def _populate(client, world, db, base) -> None:
    """A student who came, checked and ran; a revealed solution; staff
    previews and the set's build beside them."""
    sign_in_as(client, STUDENT)
    client.get(f"{base}/1")
    client.post(f"{base}/1/check", data={"source": "print(42)"})
    client.post(f"{base}/1/run", data={"source": "print(42)", "stdin": "1"})
    sign_in_as(client, TEACHER)
    assert client.post(f"{base}/reveal", data={"n": "1"}).status_code == 200


async def test_deleting_a_session_takes_its_work_and_nothing_else(
    client, world, db, ready, session
):
    set_id, version_id, preview = ready
    session_id, base = session
    other = _opened(client, world, set_id, label="Lab B")
    other_base = f"/courses/{world['course']}/sessions/{other}"
    _populate(client, world, db, base)
    _populate(client, world, db, other_base)
    client.post(f"{preview}/1/run", data={"source": "print(1)"})

    response = client.post(f"{base}/delete", data={"confirm": " Lab A "})
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"].endswith(
        f"/courses/{world['course']}/exercises/{set_id}"
    )
    assert _rows(db, ExerciseSession, ExerciseSession.c.id == session_id) == []
    for table in (ExerciseJob, ExerciseSessionParticipant, ExerciseSessionReveal):
        assert _rows(db, table, table.c.session_id == session_id) == []
        assert _rows(db, table, table.c.session_id == other) != []
    # The set, its build and the preview are the set's, not the session's.
    assert _count(db, ExerciseJob, ExerciseJob.c.session_id.is_(None)) == 2
    assert [v.id for v in _versions(db, set_id)] == [version_id]


async def test_deleting_a_session_asks_for_its_label(client, world, db, session):
    session_id, base = session
    response = client.post(f"{base}/delete", data={"confirm": "Lab"})
    assert response.status_code == 200
    assert "Nothing was changed" in response.text
    assert _session(db, session_id).label == "Lab A"
    page = client.get(base).text
    assert 'data-expected="Lab A"' in page


async def test_deleting_a_session_is_staff_only(client, world, db, session):
    session_id, base = session
    sign_in_as(client, STUDENT)
    assert client.post(f"{base}/delete", data={"confirm": "Lab A"}).status_code == 403
    sign_in_as(client, OTHER_TEACHER)
    other = f"/courses/{world['other']}/sessions/{session_id}/delete"
    assert client.post(other, data={"confirm": "Lab A"}).status_code == 404
    assert _session(db, session_id).label == "Lab A"


async def test_deleting_a_course_takes_its_exercises_and_a_late_result_lands_nowhere(
    client, world, db, ready, session
):
    """The course's cascade reaches the exercise tables, and a judge that was
    running one of the course's jobs finishes it into zero rows rather than
    into a foreign key violation - the reason results are a column."""
    _, _, preview = ready
    _, base = session
    _populate(client, world, db, base)
    client.post(f"{preview}/1/check", data={"source": "print(1)"})
    _imported(client, world)  # a second set, still building
    with db.begin() as conn:
        running = conn.execute(
            update(ExerciseJob)
            .where(ExerciseJob.c.kind == ExerciseJobKind.check)
            .values(status=ExerciseJobStatus.running, started_at=func.now())
            .returning(ExerciseJob.c.id)
        ).scalars().all()
    assert len(running) == 2  # the student's and the preview's

    sign_in_as(client, ADMIN)
    response = client.post(
        f"/courses/{world['course']}/delete", data={"confirm": "Programming 1"}
    )
    assert response.status_code == 204, response.text
    for table in (
        ExerciseJob,
        ExerciseSessionParticipant,
        ExerciseSessionReveal,
        ExerciseSession,
        ExerciseSetVersion,
        ExerciseSet,
    ):
        assert _count(db, table) == 0, table.name

    result = {"kind": "error", "message": "late"}
    with db.begin() as conn:
        for job_id in running:
            repo.finish_exercise_job(conn, job_id, result)
    assert _count(db, ExerciseJob) == 0


async def test_an_archived_course_runs_no_exercise(client, world, db, ready, session):
    """The third of archiving's enforcement points: no Run or Check in a
    session, and no staff preview either. Reading still works."""
    _, _, preview = ready
    _, base = session
    with db.begin() as conn:
        conn.execute(
            update(Course)
            .where(Course.c.id == world["course"])
            .values(archived_at=func.now())
        )
    response = client.post(f"{preview}/1/check", data={"source": "print(1)"})
    assert response.status_code == 200
    assert "This course is archived" in response.text

    sign_in_as(client, STUDENT)
    assert client.get(f"{base}/1").status_code == 200
    for button in ("check", "run"):
        response = client.post(f"{base}/1/{button}", data={"source": "print(1)"})
        assert response.status_code == 200
        assert "This course is archived" in response.text
    assert _count(db, ExerciseJob, ExerciseJob.c.kind != ExerciseJobKind.build) == 0
