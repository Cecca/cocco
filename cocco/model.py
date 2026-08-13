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
    """What someone is *on a course*. Every value here is a per-course thing:
    the site-wide power to bring a course into being lives in `Administrator`,
    which names no course because a course being created does not exist yet."""

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
    # Shut out of the site entirely, and why. Columns on the row rather than a
    # table of their own - the opposite of `Administrator`, and for the opposite
    # reason: that one has to be able to name someone who has never signed in,
    # while a block always names someone who already did something, so the
    # account exists. Putting it here means `get_user` - which already reads
    # this row on every single request - asks the question for free, and a block
    # takes effect on the blocked person's very next click.
    Column("blocked_at", DateTime, nullable=True),
    # Written by an administrator and shown to the blocked person, so it is the
    # sentence they read rather than a note for a log.
    Column("blocked_reason", String, nullable=True),
)


ApiToken = Table(
    "api_token",
    metadata,
    Column("id", Integer, primary_key=True),
    # Keyed on `user.id` and not on the address, the opposite of
    # `Administrator` and for the opposite reason: that table has to be able to
    # name somebody who has never signed in, while a token is minted on its
    # owner's own profile page, so the account always exists.
    Column("user_id", Integer, ForeignKey("user.id"), nullable=False),
    # What the token is for, in its owner's words. Shown in the list so that
    # revoking the right one is possible months later.
    Column("label", String, nullable=False),
    # The SHA-256 of the token, never the token: the hash is the lookup key, so
    # a leaked database yields nothing that can be presented to the API. This
    # is why the value is shown exactly once, when it is minted - there is
    # nowhere it could be read back from.
    Column("token_hash", String, nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
    # Written on every accepted call. One UPDATE per API request is nothing
    # against the work the request itself does, and it is the only thing that
    # can tell its owner a token is stale before they delete it.
    Column("last_used_at", DateTime, nullable=True),
)


Administrator = Table(
    "administrator",
    metadata,
    # Keyed by the address rather than by `user.id`, because an administrator is
    # designated *before* they have ever signed in: `cocco.toml` names the first
    # set of them and the account only appears on the first accepted callback
    # from Google. An address is what Google proves and what `user.email` is
    # unique on, so it identifies a person here just as well as an id would.
    #
    # Stored normalized - lowercased and trimmed, the form `normalize_email`
    # produces and the form `user.email` already holds - so the lookup is a
    # plain equality.
    Column("email", String, primary_key=True),
)


# TODO: add the year of the course
# TODO: store a slug of the course
Course = Table(
    "course",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", String, nullable=False),
    # The shared secret a student has to quote to enroll, or NULL for a course
    # anyone signed in may join. Plaintext deliberately: course staff read it
    # back off the editor to announce it to the class, which is the whole point
    # of a key like this, and it guards a class list rather than an account.
    # Only ever *printed* by the course editor, which is staff-only - every
    # other query reports whether it is set, never what it is.
    Column("enrollment_key", String, nullable=True),
    # When the course was archived, or NULL for a live one. Archiving is a
    # visibility state and not a lock: the people already enrolled go on
    # reading it and stop being able to submit, everybody else stops being
    # shown it, and nobody new can join. A timestamp rather than a flag, for
    # `blocked_at`'s reason - the moment is worth keeping and NULL is the whole
    # of "no".
    Column("archived_at", DateTime, nullable=True),
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
    # Where the challenge sits on its course page, the way `execution_order`
    # places a test case within its challenge. Course staff reorder them in the
    # course editor. Ties are broken by id everywhere this is ordered by, so a
    # course whose challenges all still sit at the default keeps the order they
    # were created in - and the arrows are the only thing that ever writes it.
    Column("position", Integer, nullable=False, default=0, server_default=text("0")),
    # As on `course`, and the two are always read together: a challenge under
    # an archived course is archived in effect even with this column NULL. That
    # `or` is spelled exactly twice, both times in `repository.py`
    # (`_EFFECTIVE_ARCHIVED_SQL` and `_effective_archived_at`), and never in a
    # route or a template - the way `effective_course_role` is spelled once.
    # This column on its own is the challenge's *own* state, which only the
    # challenge editor's danger zone reads.
    Column("archived_at", DateTime, nullable=True),
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
