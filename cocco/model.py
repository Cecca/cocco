from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cocco.evaluator import Evaluator, evaluator_adapter


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


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(nullable=False)
    role: Mapped[UserRole] = mapped_column(nullable=False)
    nickname: Mapped[str] = mapped_column(nullable=False)


class Course(Base):
    __tablename__ = "course"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(nullable=False)


class Challenge(Base):
    __tablename__ = "challenge"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(nullable=False)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id"), nullable=True)
    max_memory_bytes: Mapped[int] = mapped_column(nullable=False)
    max_time_s: Mapped[float] = mapped_column(nullable=False)
    max_threads: Mapped[int] = mapped_column(nullable=False, default=1)


class TestCase(Base):
    __tablename__ = "testcase"
    id: Mapped[int] = mapped_column(primary_key=True)
    challenge_id: Mapped[int | None] = mapped_column(ForeignKey("challenge.id"), nullable=True)
    execution_order: Mapped[int] = mapped_column(nullable=False)
    input_path: Mapped[str] = mapped_column(nullable=False)
    expected_output_path: Mapped[str] = mapped_column(nullable=False)


class Submission(Base):
    __tablename__ = "submission"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    challenge_id: Mapped[int] = mapped_column(ForeignKey("challenge.id"))
    status: Mapped[SubmissionStatus] = mapped_column(nullable=False, default=SubmissionStatus.queued)
    enqueued: Mapped[datetime] = mapped_column(nullable=False, default=datetime.now)
    language: Mapped[str] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)


class TestOutcome(Base):
    __tablename__ = "testoutcome"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submission.id"), nullable=True)
    testcase_id: Mapped[int | None] = mapped_column(ForeignKey("testcase.id"), nullable=True)
    outcome: Mapped[OutcomeEnum] = mapped_column(nullable=False)
    elapsed_s: Mapped[float | None] = mapped_column(nullable=True)
    peak_memory_bytes: Mapped[int | None] = mapped_column(nullable=True)
    score: Mapped[float | None] = mapped_column(nullable=True)
    message: Mapped[str | None] = mapped_column(nullable=True)
    stdout: Mapped[str | None] = mapped_column(nullable=True)
    stderr: Mapped[str | None] = mapped_column(nullable=True)
