from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
)
from sqlalchemy.types import TypeDecorator

from cocco.evaluator import Evaluator, evaluator_adapter


metadata = MetaData()


class UserRole(str, Enum):
    admin = "admin"
    teacher = "teacher"
    student = "student"


class SubmissionStatus(Enum):
    queued = "queued"
    running = "running"
    done = "done"


class OutcomeEnum(Enum):
    success = "success"
    compile_failure = "compile_failure"
    timeout = "timeout"
    out_of_memory = "out_of_memory"
    runtime_error = "runtime_error"
    judge_internal_error = "judge_internal_error"


class EvaluatorType(TypeDecorator):
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Evaluator | None, dialect):
        return None if value is None else value.model_dump()

    def process_result_value(self, value, dialect):
        return None if value is None else evaluator_adapter.validate_python(value)


users = Table(
    "user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String, nullable=False),
    Column("role", SAEnum(UserRole), nullable=False),
    Column("nickname", String, nullable=False),
)


courses = Table(
    "course",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", String, nullable=False),
)


challenges = Table(
    "challenge",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", String, nullable=False),
    Column("course_id", Integer, ForeignKey("course.id"), nullable=True),
    Column("max_memory_bytes", Integer, nullable=False),
    Column("max_time_s", Float, nullable=False),
    Column("max_threads", Integer, nullable=False, default=1),
)


test_cases = Table(
    "testcase",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("challenge_id", Integer, ForeignKey("challenge.id"), nullable=True),
    Column("execution_order", Integer, nullable=False),
    Column("input_path", String, nullable=False),
    Column("expected_output_path", String, nullable=False),
)


submissions = Table(
    "submission",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("user.id"), nullable=True),
    Column("challenge_id", Integer, ForeignKey("challenge.id"), nullable=True),
    Column(
        "status",
        SAEnum(SubmissionStatus),
        nullable=False,
        default=SubmissionStatus.queued,
    ),
    Column("enqueued", DateTime, nullable=False, default=datetime.now),
    Column("language", String, nullable=False),
    Column("source", String, nullable=False),
)


test_outcomes = Table(
    "testoutcome",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("submission_id", Integer, ForeignKey("submission.id"), nullable=True),
    Column("testcase_id", Integer, ForeignKey("testcase.id"), nullable=True),
    Column("outcome", SAEnum(OutcomeEnum), nullable=False),
    Column("elapsed_s", Float, nullable=True),
    Column("peak_memory_bytes", Integer, nullable=True),
    Column("score", Float, nullable=True),
    Column("message", String, nullable=True),
    Column("stdout", String, nullable=True),
    Column("stderr", String, nullable=True),
)


