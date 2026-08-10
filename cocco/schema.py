"""\
This module contains Pydantic schemas for user input validation, or the output
of complex database queries.
"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

from cocco.model import OutcomeEnum, SubmissionStatus


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)    


class User(Base):
    id: int | None
    email: str
    nickname: str


class Course(Base):
    id: int | None = None
    title: str
    description: str


class Challenge(Base):
    id: int | None = None
    course_id: int | None = None
    max_threads: int = 1
    title: str
    description: str
    max_memory_bytes: int
    max_time_s: float


class TestCase(Base):
    id: int | None = None
    challenge_id: int | None = None
    execution_order: int
    input_path: str
    expected_output_path: str
    is_sample: bool = False


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
    
