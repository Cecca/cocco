from datetime import datetime
from enum import Enum
from sqlmodel import JSON, Field, SQLModel, Session, TypeDecorator, insert

from cocco.evaluator import Evaluator, evaluator_adapter


class UserRole(str, Enum):
    admin = "admin"
    teacher = "teacher"
    student = "student"


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str
    role: UserRole
    nickname: str   # Automatically generated with cute adjectives and animals
    

class Course(SQLModel, table=True):
    """A course is a collection of challenges"""
    id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str


class EvaluatorType(TypeDecorator):
      impl = JSON                                                                                        
      cache_ok = True

      def process_bind_param(self, value: Evaluator | None, dialect):
          return None if value is None else value.model_dump()
                                                                                                         
      def process_result_value(self, value, dialect):
          return None if value is None else evaluator_adapter.validate_python(value)   


class Challenge(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str
    course_id: int | None = Field(default=None, foreign_key="course.id")
    max_memory_bytes: int
    max_time_s: float
    max_threads: int = Field(default=1)


class TestCase(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    challenge_id: int | None = Field(foreign_key="challenge.id")
    execution_order: int
    input_path: str
    expected_output_path: str


class SubmissionStatus(Enum):
    queued = "queued"
    running = "running"
    done = "done"


class Submission(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id")
    challenge_id: int | None = Field(default=None, foreign_key="challenge.id")
    status: SubmissionStatus = Field(default=SubmissionStatus.queued)
    enqueued: datetime = Field(default_factory=datetime.now)
    language: str
    source: str     # for now we support only single-file submissions


class OutcomeEnum(Enum):
    success = "success"
    compile_failure = "compile_failure"
    timeout = "timeout"
    out_of_memory = "out_of_memory"
    runtime_error = "runtime_error"
    judge_internal_error = "judge_internal_error"


class TestOutcome(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    submission_id: int | None = Field(default=None, foreign_key="submission.id")
    testcase_id: int | None = Field(default=None, foreign_key="testcase.id")
    outcome: OutcomeEnum
    elapsed_s: float | None = None
    peak_memory_bytes: int | None = None
    score: float | None = None
    message: str | None = None
    stdout: str | None = None
    stderr: str | None = None


