"""What `model.py` declares, asked of the database the migrations built."""

import warnings

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError, SAWarning

from cocco.model import (
    Course,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseSession,
    ExerciseSessionMode,
    ExerciseSet,
    ExerciseSetVersion,
    User,
)


def test_an_exercise_set_gets_an_id(db):
    """`exercise_set.id` is in a composite foreign key, which turns SQLAlchemy's
    `autoincrement="auto"` off; the sequence the migration made is then
    invisible to the drift test and an insert reports `(None,)` with a
    warning. `autoincrement=True` in the model is what this pins."""
    with db.begin() as conn:
        conn.execute(insert(Course).values(title="c", description="d"))
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            result = conn.execute(insert(ExerciseSet).values(course_id=1))
        assert result.inserted_primary_key == (1,)


def _version_with_a_session(conn) -> None:
    conn.execute(
        insert(User).values(email="a@x", nickname="a", image=b"", image_mime="x")
    )
    conn.execute(insert(Course).values(title="c", description="d"))
    conn.execute(insert(ExerciseSet).values(course_id=1))
    conn.execute(insert(ExerciseSetVersion).values(set_id=1, source="s", title="t"))
    conn.execute(
        insert(ExerciseSession).values(
            set_id=1,
            version_id=1,
            label="l",
            mode=ExerciseSessionMode.live,
            unlocked_through=1,
        )
    )


def _job(conn, kind, *, session_id=1, exercise_index=1) -> None:
    conn.execute(
        insert(ExerciseJob).values(
            kind=kind,
            user_id=1,
            version_id=1,
            session_id=session_id,
            exercise_index=exercise_index,
            source="print(1)",
            stdin="" if kind is ExerciseJobKind.run else None,
        )
    )


@pytest.mark.parametrize("session_id", [1, None], ids=["session", "preview"])
def test_a_second_queued_job_for_the_same_button_conflicts(db, session_id):
    """The same person, kind, session and exercise: the index refuses it, so
    the route can replace the queued one with an upsert - and a preview, whose
    `session_id` is NULL, dedupes too (NULLS NOT DISTINCT)."""
    with db.begin() as conn:
        _version_with_a_session(conn)
        _job(conn, ExerciseJobKind.run, session_id=session_id)
    with pytest.raises(IntegrityError), db.begin() as conn:
        _job(conn, ExerciseJobKind.run, session_id=session_id)


def test_a_run_never_takes_the_place_of_a_check(db):
    """A queued Check is a recorded attempt: a Run on the same exercise, or
    either kind on another exercise, is a different queued job."""
    with db.begin() as conn:
        _version_with_a_session(conn)
        _job(conn, ExerciseJobKind.check)
        _job(conn, ExerciseJobKind.run)
        _job(conn, ExerciseJobKind.check, exercise_index=2)
        _job(conn, ExerciseJobKind.run, session_id=None)
