"""\
This module contains Pydantic schemas for user input validation.
"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

from cocco.model import OutcomeEnum, SubmissionStatus, UserRole


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)    


class User(Base):
    id: int | None = None
    email: str
    role: UserRole
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


class Submission(Base):
    id: int | None = None
    user_id: int | None = None
    challenge_id: int | None = None
    status: SubmissionStatus = SubmissionStatus.queued
    enqueued: datetime = Field(default_factory=datetime.now)
    language: str
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

