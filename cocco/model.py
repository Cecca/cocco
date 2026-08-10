from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    text,
)
from sqlalchemy.types import TypeDecorator

from cocco.evaluator import Evaluator, evaluator_adapter


class UserRole(str, Enum):
    admin = "admin"
    teacher = "teacher"
    collaborator = "collaborator"
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


metadata = MetaData()


User = Table(
    "user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String, nullable=False),
    Column("nickname", String, nullable=False),
    Column("image", LargeBinary, nullable=False),
    Column("image_mime", String, nullable=False),
)


# TODO: add the year of the course
# TODO: store a slug of the course
Course = Table(
    "course",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", String, nullable=False),
)


Enrollment = Table(
    "enrollment",
    metadata,
    Column("user_id", Integer, ForeignKey("user.id"), primary_key=True),
    Column("course_id", Integer, ForeignKey("course.id"), primary_key=True),
    Column("role", SAEnum(UserRole), nullable=False),
)


# TODO: store a slug of the challenge
Challenge = Table(
    "challenge",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", String, nullable=False),
    Column("course_id", Integer, ForeignKey("course.id"), nullable=True),
    Column("max_memory_bytes", Integer, nullable=False),
    Column("max_time_s", Float, nullable=False),
    Column("max_threads", Integer, nullable=False, default=1),
    Column("evaluator", EvaluatorType, nullable=False),
)


TestCase = Table(
    "testcase",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("challenge_id", Integer, ForeignKey("challenge.id"), nullable=True),
    Column("execution_order", Integer, nullable=False),
    Column("input_path", String, nullable=False),
    Column("expected_output_path", String, nullable=False),
    # Sample test cases are public: their input and expected output can be
    # downloaded from the challenge page. All the others are hidden.
    Column("is_sample", Boolean, nullable=False, default=False),
    # A retired test case keeps its files and its past outcomes, but no new
    # submission is graded on it and it is no longer public. This is what course
    # staff reach for instead of deleting: an outcome must keep pointing at the
    # test case it was scored on.
    Column(
        "is_active", Boolean, nullable=False, default=True, server_default=text("true")
    ),
)


Submission = Table(
    "submission",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("user.id"), nullable=False),
    Column("challenge_id", Integer, ForeignKey("challenge.id"), nullable=False),
    Column(
        "status",
        SAEnum(SubmissionStatus),
        nullable=False,
        default=SubmissionStatus.queued,
    ),
    Column("enqueued", DateTime, nullable=False, default=datetime.now),
    Column("language", String, nullable=False),
    # The name of the file the student uploaded, shown in the submission history
    Column("filename", String, nullable=False),
    Column("source", String, nullable=False),
)


TestOutcome = Table(
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
