from datetime import datetime
from enum import Enum

from sqlalchemy import (
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


class UserRole(str, Enum):
    admin = "admin"
    teacher = "teacher"
    collaborator = "collaborator"
    student = "student"


class SubmissionStatus(Enum):
    queued = "queued"
    running = "running"
    done = "done"


class LeaderboardRanking(str, Enum):
    """What "best" means on a challenge's leaderboard.

    `running_time` is the speed board: only a submission that passed every test
    qualifies, and the fastest one wins. The two score boards admit partially
    correct submissions - that is the whole point of running a challenge on a
    partial-credit evaluator - and rank them highest first.
    """

    running_time = "running_time"
    total_score = "total_score"
    average_score = "average_score"


class OutcomeEnum(Enum):
    success = "success"
    compile_failure = "compile_failure"
    timeout = "timeout"
    out_of_memory = "out_of_memory"
    runtime_error = "runtime_error"
    judge_internal_error = "judge_internal_error"
    # The challenge's evaluator is what broke, not the submission. Kept apart
    # from `judge_internal_error` because this is the one failure course staff
    # can actually fix, and apart from `timeout`/`runtime_error` because those
    # are verdicts on the student's program - a checker that times out must
    # never be reported as the submission timing out.
    evaluator_error = "evaluator_error"


metadata = MetaData()


User = Table(
    "user",
    metadata,
    Column("id", Integer, primary_key=True),
    # The login identifier: Google proves an address, and the address is what
    # we look the account up by. Two rows for one address would make "which
    # user is this?" unanswerable.
    Column("email", String, nullable=False, unique=True),
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
    # The challenge's checker. NULL means the bundled default (exact match),
    # read straight out of `cocco/evaluators`; otherwise a path, relative to
    # the storage root, to a script course staff uploaded. Its language comes
    # from its extension, exactly as a submission's does.
    Column("evaluator_path", String, nullable=True),
    # Which leaderboard the challenge page shows. Course staff pick it in the
    # editor; the judge is unaware of it.
    Column(
        "ranking",
        SAEnum(LeaderboardRanking),
        nullable=False,
        default=LeaderboardRanking.running_time,
        server_default=text("'running_time'"),
    ),
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
