from datetime import datetime
from enum import Enum
from sqlmodel import Field, SQLModel, Session, insert


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


class Submission(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id")
    challenge_id: int | None = Field(default=None, foreign_key="challenge.id")
    language: str   # TODO: maybe here we should support an explicit set of allowed languages
    source: str     # for now we support only single-file submissions


class OutcomeEnum(Enum):
    success = "success"
    failure = "failure"
    compile_failure = "compile_failure"


class TestOutcome(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    submission_id: int | None = Field(default=None, foreign_key="submission.id")
    testcase_id: int | None = Field(default=None, foreign_key="testcase.id")
    outcome: OutcomeEnum
    elapsed_s: float | None
    peak_memory_bytes: int | None


class QueuedSubmission(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    enqueued: datetime
    submission_id: int | None = Field(default=None, foreign_key="submission.id")


def enqueue_submission(submission: Submission, session: Session):
    q = QueuedSubmission(enqueued=datetime.now(), submission_id=submission.id)
    session.add(q)
    
    

