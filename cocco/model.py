from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


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


class ExerciseVersionStatus(Enum):
    """Where an imported exercise file is in its one and only build.

    A version is created `building` and the build moves it, exactly once, to
    `ready` or `failed`. Only a `ready` one can become a set's current version
    or be what a session is opened on.
    """

    building = "building"
    ready = "ready"
    failed = "failed"


class ExerciseSessionMode(Enum):
    """`live` is paced by the teacher, up to `unlocked_through`; `open` has
    everything unlocked - homework, or a live session after class."""

    live = "live"
    open = "open"


class ExerciseJobKind(Enum):
    """The three kinds of work an exercise asks of the judge.

    `build` computes a version's expected outputs, for staff; `check` is a
    recorded attempt, the only kind progress is ever derived from; `run` is the
    student's own stdin, nothing compared.
    """

    build = "build"
    check = "check"
    run = "run"


class ExerciseJobStatus(Enum):
    queued = "queued"
    running = "running"
    done = "done"


metadata = MetaData()


# Every timestamp in this schema is `timestamptz`, and every one of them is
# written by Postgres rather than by a Python process. A naive column only means
# something if you already know which machine's wall clock wrote it, and
# `cocco-web` and `cocco-judge` are two processes with two clocks - so
# `submission.enqueued` and `submission.started_at` could disagree about which
# came first. `now()` is one clock, and it is the *transaction's*, so every
# timestamp a single statement writes is the same instant.
#
# The cost is paid in `migrations/`: `compare_server_default` is off in
# `migrate.COMPARE_OPTIONS` - see the reasoning there - so a `server_default` is
# invisible to autogenerate and to the drift test, and every revision that adds
# one of these columns has to carry it by hand.


def created_at() -> Column:
    """When the row came into being. Never NULL, never rewritten.

    A factory rather than a shared constant, because a `Column` belongs to
    exactly one `Table`. The point of it being here at all is that the rule -
    `timestamptz`, NOT NULL, `now()` - is spelled once and so cannot drift
    between the six tables that carry it.
    """
    return Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def updated_at() -> Column:
    """When the row last *changed* - which is what it claims, and all it claims.

    `onupdate` fires on every Core UPDATE against the table, so a write path
    added later cannot forget it; the price is that it does not distinguish "a
    teacher edited this" from "the course editor's arrows renumbered it" or
    "this account signed in". That is the honest reading of the column, and one
    declaration here beats every call site remembering.

    One write escapes it and has to say so itself: SQLAlchemy fires `onupdate`
    for an UPDATE and not for an `INSERT ... ON CONFLICT DO UPDATE`, which is
    why `set_enrollment_role` in `repository/courses.py` names this column in its `set_`.
    """
    return Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


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
    Column("blocked_at", DateTime(timezone=True), nullable=True),
    # Written by an administrator and shown to the blocked person, so it is the
    # sentence they read rather than a note for a log.
    Column("blocked_reason", String, nullable=True),
    # When the account came into being - deliberately *not* the same question as
    # when its owner first signed in. `get_or_create_user` is reached from two
    # places: Google's callback, and the course people page, where a teacher
    # writes out a roster before term starts. So a row here can belong to
    # somebody who has never been near the login page, and `first_login_at IS
    # NULL` is exactly that person.
    created_at(),
    updated_at(),
    # Written by `record_sign_in`, called from `_complete_sign_in` *after* the
    # blacklist refusal - a sign-in that was turned away is not one. The first
    # is set once and never again, through a `coalesce` in the same statement
    # that writes the second, so there is no read for two tabs to race on.
    Column("first_login_at", DateTime(timezone=True), nullable=True),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
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
    created_at(),
    # Written on every accepted call. One UPDATE per API request is nothing
    # against the work the request itself does, and it is the only thing that
    # can tell its owner a token is stale before they delete it.
    Column("last_used_at", DateTime(timezone=True), nullable=True),
)


# A password, for the deployments that turn `[auth] password_login` on. Its own
# table and not a nullable column on `user`, for one reason above all the
# others: `_USER_COLUMNS` in `repository/users.py` is read on every single request,
# and a credential has no business travelling with the identity that every
# route already holds. Keyed on `user.id` for `ApiToken`'s reason - a password
# belongs to an account that exists, unlike `Administrator`, which is keyed on
# an address precisely so it can name somebody who has never signed in.
#
# The row is the whole of the feature's state: no row means no password, which
# is what a Google-only account looks like and what an account looks like
# before anybody has claimed it.
UserPassword = Table(
    "user_password",
    metadata,
    # The primary key, so an account has at most one password and the upsert in
    # `set_user_password` has something to conflict on.
    Column("user_id", Integer, ForeignKey("user.id"), primary_key=True),
    # `scrypt$n$r$p$<salt>$<key>` - see `cocco/passwords.py`, which owns the
    # shape. Self-describing, so the parameters can be raised without
    # invalidating what is already stored.
    Column("password_hash", String, nullable=False),
    created_at(),
    # Bumped whenever the password changes, which is the one thing an owner
    # might want to know about a credential they cannot read back.
    updated_at(),
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
    # When they were appointed. There is no `updated_at`: the table has exactly
    # one other column and it is the key, so there is nothing here to change -
    # an administrator is added or DELETEd, never edited.
    created_at(),
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
    Column("archived_at", DateTime(timezone=True), nullable=True),
    created_at(),
    updated_at(),
)


Enrollment = Table(
    "enrollment",
    metadata,
    Column("user_id", Integer, ForeignKey("user.id"), primary_key=True),
    Column("course_id", Integer, ForeignKey("course.id"), primary_key=True),
    Column("role", SAEnum(UserRole), nullable=False),
    # When they joined the course, and when the row last moved - which for this
    # table means the role changed, since the two other columns are the key.
    # Both survive a `delete_enrollment` and a re-enrolment as new values: the
    # row is gone and the one that replaces it is a new fact, not the old one
    # returning.
    created_at(),
    updated_at(),
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
    # `or` is spelled exactly once, in `repository/rules.py`'s
    # `_effective_archived_at`, and never in a route or a template - the way
    # `effective_course_role` is spelled once.
    # This column on its own is the challenge's *own* state, which only the
    # challenge editor's danger zone reads.
    Column("archived_at", DateTime(timezone=True), nullable=True),
    # The other end of the life cycle, and `archived_at` inverted: when this
    # challenge became - or becomes - visible to the class. NULL is hidden,
    # a past instant is visible, a future one is a challenge that opens by
    # itself at that moment, with no one having to be at a keyboard for it.
    #
    # Deliberately no `server_default`: a *new* challenge is hidden, because a
    # challenge with no test cases and no evaluator has no business being on a
    # student's course page. The migration that adds this column backfills
    # every existing row from `created_at` for exactly that reason.
    #
    # Unlike `archived_at` it does not cascade from the course, which has no
    # hidden state to cascade. The rule is still spelled exactly once, in
    # `repository/rules.py`'s `_challenge_visible`, and never in a route or a
    # template.
    Column("visible_from", DateTime(timezone=True), nullable=True),
    created_at(),
    # Note what this does not distinguish: `reorder_challenges` renumbers every
    # challenge of a course, so moving one with the arrows bumps all of them.
    # See `updated_at` above - the column says the row changed, and it did.
    updated_at(),
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
    # No `updated_at` beside it, unlike `challenge`: a test case's files are
    # replaced by deleting the row and inserting another (see `plan_testcases`),
    # and `swap_testcase_order` rewrites `execution_order` on rows nobody
    # touched. There would be nothing an `updated_at` here could be read for.
    created_at(),
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
    # This table's `created_at`, under the better name: the moment the row
    # joined the queue. `started_at` and `finished_at` are the other two thirds
    # of the judging lifecycle - when a judge claimed it in
    # `pull_queued_submission`, and when `mark_submission_done` let go. Both are
    # NULL for a queued submission and both are put *back* to NULL by
    # `regrade_challenge`, which returns the row to the queue.
    Column(
        "enqueued", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
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


# --- Exercises ---------------------------------------------------------------
#
# Interactive exercises are their own tables and share nothing with challenges
# above the judge's execution layer. There is no `exercise` table and no
# `exercise_test`: the `.md` file a teacher imports is the source of truth, and
# everything about an exercise lives in the version compiled from it. An
# exercise is therefore identified by its 1-based position in that version,
# `exercise_index`, wherever a row has to name one.
#
# No foreign key below declares an `ON DELETE`, like every other one in this
# file: `delete_course`'s hand-written cascade takes them in order - jobs,
# reveals, participants, sessions, then the set's `current_version_id` set to
# NULL, versions, sets. Deleting a set is the same list from its sessions down.
# Either way the jobs go **by `version_id`**, not by session: a build and a
# staff preview have no session, and a cascade that deleted jobs by session
# would leave them holding the versions down.


ExerciseSet = Table(
    "exercise_set",
    metadata,
    # `autoincrement=True` by hand, and not optional: SQLAlchemy's "auto" only
    # makes a SERIAL of a key column that is in no foreign key, and `id` is in
    # `exercise_set_current_version_fkey` below. Left to "auto", the database
    # still gets its sequence from the migration, but an `insert(ExerciseSet)`
    # reports no `inserted_primary_key` - a difference the drift test cannot see.
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("course_id", Integer, ForeignKey("course.id"), nullable=False),
    # The latest *ready* version, or NULL until a first build succeeds. Moved
    # only by a build that finishes `ready`, so a re-import that fails leaves
    # everything using the version that worked.
    #
    # The set and its versions point at each other, so this foreign key cannot
    # exist when either table is created: `use_alter` has it added afterwards,
    # and the cascade has to clear it before the versions can go. It is on
    # `(id, current_version_id)` rather than the version alone, which is what
    # makes "the current version is one of *this* set's" the database's rule
    # and not a route's. A NULL in either column skips the check, as a plain
    # nullable foreign key does.
    Column("current_version_id", Integer, nullable=True),
    # As on `course`. An archived set's course page entry goes, and nothing is
    # opened on it any more.
    Column("archived_at", DateTime(timezone=True), nullable=True),
    created_at(),
    updated_at(),
    ForeignKeyConstraint(
        ["id", "current_version_id"],
        ["exercise_set_version.set_id", "exercise_set_version.id"],
        name="exercise_set_current_version_fkey",
        use_alter=True,
    ),
)


# One import of a set's `.md` file, and immutable: the source is written when the
# row is created, and the compiled document, the problems and the status are
# written exactly once, together, by the build that finishes it. Two values that
# are both written once and never updated cannot drift apart. That is also why
# there is no `updated_at`: the only write after the insert is the build's, and
# when that happened is its job's `finished_at` - the `exercise_job` row with
# `kind = 'build'` and this `version_id`.
ExerciseSetVersion = Table(
    "exercise_set_version",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("set_id", Integer, ForeignKey("exercise_set.id"), nullable=False),
    # The file verbatim, solutions included. Only staff ever see it.
    Column("source", String, nullable=False),
    # From the front matter, at import - so that a set can be listed without
    # reading a document that does not exist until the build is done.
    Column("title", String, nullable=False),
    Column(
        "status",
        SAEnum(ExerciseVersionStatus),
        nullable=False,
        default=ExerciseVersionStatus.building,
    ),
    # A list of `schema.ExerciseProblem`, written by the build: NULL exactly
    # while the version is building, a list - possibly empty - once it is not.
    Column("problems", JSONB, nullable=True),
    # A `schema.ExerciseDocument`, expected outputs filled in. Every reader of
    # a version's content goes through this, never through `source`.
    Column("document", JSONB, nullable=True),
    created_at(),
    # What the composite foreign keys on `exercise_set` and `exercise_session`
    # refer to. Redundant as a uniqueness rule, `id` alone being the key, and
    # there for that reason only.
    UniqueConstraint("set_id", "id", name="exercise_set_version_set_id_id_key"),
    CheckConstraint(
        "(status = 'building') = (problems IS NULL)",
        name="exercise_set_version_problems_once_built",
    ),
    CheckConstraint(
        "status <> 'ready' OR document IS NOT NULL",
        name="exercise_set_version_ready_has_document",
    ),
)


# One run of a set with one group of students: its own pacing, its own progress.
ExerciseSession = Table(
    "exercise_session",
    metadata,
    Column("id", Integer, primary_key=True),
    # No foreign key of its own: the one below goes through a version of this
    # set, whose own `set_id` already has to name a set that exists.
    Column("set_id", Integer, nullable=False),
    # The version this session shows, pinned when it is opened. Moved only by
    # "switch to the latest version", and the foreign key is on `(set_id,
    # version_id)`, so it can only ever be moved to a version of its own set.
    Column("version_id", Integer, nullable=False),
    # "Lab A - 20 Oct", in the teacher's words.
    Column("label", String, nullable=False),
    Column("mode", SAEnum(ExerciseSessionMode), nullable=False),
    # How many exercises a `live` session has unlocked, counted from the first;
    # ignored in `open` mode. The upper bound is the version's exercise count,
    # which lives in a JSONB document and so is the routes' to clamp.
    Column("unlocked_through", Integer, nullable=False),
    # This table's `created_at`, under the better name, as `submission.enqueued`
    # is that table's. NULL `closed_at` is a session still accepting work.
    Column(
        "opened_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    updated_at(),
    ForeignKeyConstraint(
        ["set_id", "version_id"],
        ["exercise_set_version.set_id", "exercise_set_version.id"],
        name="exercise_session_version_fkey",
    ),
    CheckConstraint(
        "unlocked_through >= 0", name="exercise_session_unlocked_through_check"
    ),
)


# Who came: written on a student's first visit with `ON CONFLICT DO NOTHING`,
# and the dashboard's denominator - not the course's enrollment, which counts
# everybody who could have.
ExerciseSessionParticipant = Table(
    "exercise_session_participant",
    metadata,
    Column("session_id", Integer, ForeignKey("exercise_session.id"), primary_key=True),
    Column("user_id", Integer, ForeignKey("user.id"), primary_key=True),
    Column(
        "joined_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)


# A solution shown to the class, per session and per exercise. The key is what
# makes revealing twice a no-op.
ExerciseSessionReveal = Table(
    "exercise_session_reveal",
    metadata,
    Column("session_id", Integer, ForeignKey("exercise_session.id"), primary_key=True),
    Column("exercise_index", Integer, primary_key=True),
    Column(
        "revealed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "exercise_index >= 1", name="exercise_session_reveal_exercise_index_check"
    ),
)


# Every piece of exercise work the judge does, one row each, result included.
# The result is JSONB on the row rather than child rows, deliberately: a judge
# finishing a job whose row was deleted meanwhile updates zero rows, where
# `insert_test_outcomes` would violate a foreign key and take the process down.
ExerciseJob = Table(
    "exercise_job",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", SAEnum(ExerciseJobKind), nullable=False),
    Column(
        "status",
        SAEnum(ExerciseJobStatus),
        nullable=False,
        default=ExerciseJobStatus.queued,
    ),
    # Who asked: the student, or for a build and a preview, the staff member.
    Column("user_id", Integer, ForeignKey("user.id"), nullable=False),
    # The version the job ran against, so a check is always read against the
    # tests it was actually graded on - even after its session has moved on.
    Column(
        "version_id", Integer, ForeignKey("exercise_set_version.id"), nullable=False
    ),
    # NULL for a build, and for a staff preview: no progress query ever sees a
    # job without a session.
    Column("session_id", Integer, ForeignKey("exercise_session.id"), nullable=True),
    # 1-based; NULL exactly for a build, which is about the whole version.
    Column("exercise_index", Integer, nullable=True),
    # The student's code; NULL exactly for a build, which runs the version's own.
    Column("source", String, nullable=True),
    # The text box a run is given up front. A check reads its stdin from the
    # tests, and a build has none.
    Column("stdin", String, nullable=True),
    # What came of it, written with `status = 'done'` and never before. The
    # status is only where the job is in the queue; the verdict - passed or
    # not, per test, and the variant for a judge that broke - is
    # this document's, whose shapes `schema.ExerciseJobResult` pins.
    Column("result", JSONB, nullable=True),
    # `submission`'s three, for `submission`'s reasons.
    Column(
        "enqueued", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    # When `source` and `stdin` were last written: at the insert, or by the
    # same button pressed again while the job was still queued, which rewrites
    # them in place and keeps `enqueued` - its place in the queue - and the
    # id. Neither of those therefore says which of a student's jobs holds the
    # code they sent last, and that is the one the student page falls back on.
    Column(
        "submitted_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "(kind = 'build') = (exercise_index IS NULL)",
        name="exercise_job_exercise_index_unless_build",
    ),
    CheckConstraint(
        "(kind = 'build') = (source IS NULL)", name="exercise_job_source_unless_build"
    ),
    CheckConstraint(
        "kind <> 'build' OR session_id IS NULL", name="exercise_job_build_no_session"
    ),
    CheckConstraint(
        "kind = 'run' OR stdin IS NULL", name="exercise_job_stdin_run_only"
    ),
    CheckConstraint("exercise_index >= 1", name="exercise_job_exercise_index_check"),
    # The status and the columns that say the same thing cannot disagree: a job
    # is done exactly when it has a result and a `finished_at`, and anything
    # past the queue has been started.
    CheckConstraint(
        "(status = 'done') = (result IS NOT NULL)", name="exercise_job_result_when_done"
    ),
    CheckConstraint(
        "(status = 'done') = (finished_at IS NOT NULL)",
        name="exercise_job_finished_when_done",
    ),
    CheckConstraint(
        "status = 'queued' OR started_at IS NOT NULL",
        name="exercise_job_started_unless_queued",
    ),
    # Progress is derived from check jobs and nothing else, so every dashboard
    # and strip query reads through this. Run jobs are kept for six months and
    # outnumber checks; the predicate keeps them out of it.
    Index(
        "ix_exercise_job_check",
        "session_id",
        "exercise_index",
        "user_id",
        postgresql_where=text("kind = 'check'"),
    ),
    # One person's jobs in one session, runs included: what the student page
    # opens on (`query_last_session_jobs`) and what their strip polls. The
    # index above leaves runs out on purpose, and six months of them are
    # exactly what a lookup by person must not scan.
    Index(
        "ix_exercise_job_session_user",
        "session_id",
        "user_id",
        "exercise_index",
        postgresql_where=text("session_id IS NOT NULL"),
    ),
    # The judge's claim: the oldest queued job of a kind.
    Index("ix_exercise_job_queue", "status", "kind", "enqueued"),
    # At most one job queued per person, per button, per exercise: pressing
    # Run again on the same exercise of the same session (or preview) replaces
    # the queued Run, so mashing it never builds a backlog - but a Run never
    # replaces a Check, nor one exercise's job another's, so a recorded
    # attempt cannot vanish and a polling tab never finds somebody else's job
    # in its place. An index rather than a check before the insert, so two
    # clicks that race cannot both win and the replacement can be one
    # `ON CONFLICT` upsert. NULLS NOT DISTINCT because a preview's
    # `session_id` is NULL and has to dedupe all the same. Builds are
    # excluded: they are staff's, per version, and two quick imports are two
    # builds.
    Index(
        "uq_exercise_job_one_queued",
        "user_id",
        "kind",
        "session_id",
        "version_id",
        "exercise_index",
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=text("status = 'queued' AND kind <> 'build'"),
    ),
)
