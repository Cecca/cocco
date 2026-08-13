"""\
This module contains Pydantic schemas for user input validation, or the output
of complex database queries.
"""
import math
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cocco.model import OutcomeEnum, SubmissionStatus


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)    


class User(Base):
    # Not optional: a `User` only ever comes from a row, either the session's
    # own or one embedded in a page query, so it is always persisted.
    id: int
    email: str
    nickname: str
    # Nullable but deliberately *not defaulted*: `get_user` refuses on these, so
    # a query that selected a trimmed column list and forgot them has to fail
    # loudly here rather than quietly report everybody as welcome.
    blocked_at: datetime | None
    blocked_reason: str | None


class Course(Base):
    id: int | None = None
    title: str
    description: str
    # Defaulted, unlike `User.blocked_at` above, and the difference is the
    # reason that one is not: `get_user` refuses on the block, so a trimmed
    # column list has to fail loudly there. Nothing refuses on a `Course` -
    # `post_course_enroll` reads `query_course_archived_at` directly - so there
    # is no door here a missing column could be walked through.
    archived_at: datetime | None = None


class Challenge(Base):
    id: int | None = None
    course_id: int | None = None
    max_threads: int = 1
    title: str
    description: str
    max_memory_bytes: int
    max_time_s: float
    # The challenge's own state, not the effective one: a challenge under an
    # archived course is archived with this still NULL. See `Challenge` in
    # `model.py` for where that `or` lives.
    archived_at: datetime | None = None


class TestCase(Base):
    id: int | None = None
    challenge_id: int | None = None
    execution_order: int
    input_path: str
    expected_output_path: str
    is_sample: bool = False
    is_active: bool = True


class Submission(Base):
    id: int | None = None
    user_id: int | None = None
    challenge_id: int | None = None
    status: SubmissionStatus = SubmissionStatus.queued
    enqueued: datetime = Field(default_factory=datetime.now)
    language: str
    filename: str
    source: str


class TestOutcome(Base):
    id: int | None = None
    submission_id: int | None = None
    testcase_id: int | None = None
    elapsed_s: float | None = None
    peak_memory_bytes: int | None = None
    score: float | None = None
    message: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    outcome: OutcomeEnum


class SubmissionRunData(Base):
    """\
    The data needed to run the submission code
    """
    submission_id: int
    language: str
    source: str
    max_threads: int
    max_memory_bytes: int
    max_time_s: float
    test_cases: list[TestCase]
    # The challenge's evaluator script, relative to the storage root. None
    # means the bundled default - see `cocco.evaluators`.
    evaluator_path: str | None = None


class EvaluatorResult(Base):
    """The single JSON object an evaluator script prints on stdout.

    `score` is conventionally in [0, 1] and higher-is-better, which is what the
    headline score and the leaderboards assume, but nothing enforces it: an
    evaluator is free to use its own scale, and then those aggregates are the
    thing that is wrong, not the score.

    It must however be finite. `json.loads` accepts Python's `Infinity` and
    `NaN` spellings, no other language emits them, and neither survives a round
    trip through anything else - so accepting them would quietly make the
    contract Python-only, which is the opposite of the point.
    """

    score: float
    message: str = ""

    @field_validator("score")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score must be a finite number")
        return value


class SubmissionWithOutcomes(Base):
    """Data on a particular submission by a user,
    along with all the outcomes on the tests, if available"""

    submission_id: int | None = None
    status: SubmissionStatus = SubmissionStatus.queued
    enqueued: datetime = Field(default_factory=datetime.now)
    language: str
    source: str
    test_cases: list[tuple[TestCase, TestOutcome | None]]


class UserFocusedChallengeInfo(Base):
    """Information related to attempts made by a user on a given challenge."""

    user: User
    challenge: Challenge
    submissions: list[SubmissionWithOutcomes]
    
