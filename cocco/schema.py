"""\
This module contains Pydantic schemas for user input validation, or the output
of complex database queries.
"""
import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cocco.exercises.compare import Rules
from cocco.model import OutcomeEnum, SubmissionStatus


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)    


class User(Base):
    # Not optional: a `User` only ever comes from a row, either the session's
    # own or one embedded in a page query, so it is always persisted.
    id: int
    email: str
    nickname: str
    # Nullable but deliberately *not defaulted*: `get_user` refuses on these, so
    # a query that selected a trimmed column list and forgot them has to fail
    # loudly here rather than quietly report everybody as welcome.
    blocked_at: datetime | None
    blocked_reason: str | None


class Course(Base):
    id: int | None = None
    title: str
    description: str
    # Defaulted, unlike `User.blocked_at` above, and the difference is the
    # reason that one is not: `get_user` refuses on the block, so a trimmed
    # column list has to fail loudly there. Nothing refuses on a `Course` -
    # `post_course_enroll` reads `query_course_archived_at` directly - so there
    # is no door here a missing column could be walked through.
    archived_at: datetime | None = None


class Challenge(Base):
    id: int | None = None
    course_id: int | None = None
    max_threads: int = 1
    title: str
    description: str
    max_memory_bytes: int
    max_time_s: float
    # The challenge's own state, not the effective one: a challenge under an
    # archived course is archived with this still NULL. See `Challenge` in
    # `model.py` for where that `or` lives.
    archived_at: datetime | None = None
    # NULL is hidden, a past instant visible, a future one scheduled. Defaulted
    # for `Course.archived_at`'s reason: nothing validates a `Challenge` on its
    # way through a door, and the gate reads `query_challenge_visibility`.
    visible_from: datetime | None = None


class TestCase(Base):
    id: int | None = None
    challenge_id: int | None = None
    execution_order: int
    input_path: str
    expected_output_path: str
    is_sample: bool = False
    is_active: bool = True


class Submission(Base):
    id: int | None = None
    user_id: int | None = None
    challenge_id: int | None = None
    status: SubmissionStatus = SubmissionStatus.queued
    enqueued: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # The other two thirds of the judging lifecycle, NULL until a judge claims
    # the row and until it lets go. Defaulted, unlike `User.blocked_at` and for
    # the reason `Course.archived_at` is: nothing refuses on them, so there is
    # no door here a trimmed column list could be walked through.
    started_at: datetime | None = None
    finished_at: datetime | None = None
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
    # The name the student uploaded, and not decoration: `javac` refuses
    # `public class Foo` in anything but `Foo.java`, so the judge writes the
    # source under this name - see `judge.source_filename`. Required, because
    # `submission.filename` is NOT NULL and a query that dropped the column
    # should fail here rather than quietly go back to grading every submission
    # as `source.<ext>`.
    filename: str
    max_threads: int
    max_memory_bytes: int
    max_time_s: float
    test_cases: list[TestCase]
    # The challenge's evaluator script, relative to the storage root. None
    # means the bundled default - see `cocco.evaluators`.
    evaluator_path: str | None = None
    # Only ever read by the log: `run_from_queue` binds it beside the
    # submission id so every line of a graded run can be filtered by challenge.
    # Defaulted, unlike `filename`, because nothing is graded differently
    # without it.
    challenge_id: int | None = None


class EvaluatorResult(Base):
    """The single JSON object an evaluator script prints on stdout.

    `score` is conventionally in [0, 1] and higher-is-better, which is what the
    headline score and the leaderboards assume, but nothing enforces it: an
    evaluator is free to use its own scale, and then those aggregates are the
    thing that is wrong, not the score.

    It must however be finite. `json.loads` accepts Python's `Infinity` and
    `NaN` spellings, no other language emits them, and neither survives a round
    trip through anything else - so accepting them would quietly make the
    contract Python-only, which is the opposite of the point.
    """

    score: float
    message: str = ""

    @field_validator("score")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score must be a finite number")
        return value


class SubmissionWithOutcomes(Base):
    """Data on a particular submission by a user,
    along with all the outcomes on the tests, if available"""

    submission_id: int | None = None
    status: SubmissionStatus = SubmissionStatus.queued
    enqueued: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    language: str
    source: str
    test_cases: list[tuple[TestCase, TestOutcome | None]]


class UserFocusedChallengeInfo(Base):
    """Information related to attempts made by a user on a given challenge."""

    user: User
    challenge: Challenge
    submissions: list[SubmissionWithOutcomes]
    


# --- Exercises ---------------------------------------------------------------
#
# The compiled document of an exercise set: what `cocco.exercises.format` parses
# out of the `.md` source, plus the expected outputs the judge's build fills in.
# It is stored whole, as one JSONB value on the version, and this is the one
# definition the parser that writes it, the build that completes it and every
# reader agree on. Solutions included: the server needs them, and no student
# is ever handed this document - see the student view in the plan.

EXERCISE_FORMAT = 1


class ExerciseProblem(Base):
    """Something wrong with an exercise file, as the author page lists it.

    An `error` stops the version from becoming ready; a `warning` is shown and
    the version is used anyway.
    """

    level: Literal["error", "warning"]
    # Where in the file: "front matter", "introduction", "file", or the exercise.
    where: str
    text: str
    # Output worth showing verbatim under the sentence, e.g. a traceback.
    detail: str | None = None
    # 1-based positions, when the problem is about one exercise or one of its
    # tests - for a page that wants to put it next to the thing, not only list it.
    exercise: int | None = None
    test: int | None = None


class ExerciseTest(Base):
    # Stdin, with a final newline unless it is empty.
    input: str
    hidden: bool = False
    # As the author wrote them. `output` None means "compute it from the
    # solution"; `error` None means the program must not raise, and "" means
    # "whatever the solution raises", which the build resolves.
    output: str | None = None
    error: str | None = None
    # The file line of the `input` block, or of the `# Title` for the one test
    # an exercise with no `input` gets (`implicit`).
    line: int
    implicit: bool = False
    # Filled by the build; None straight out of the parser.
    expected_output: str | None = None
    expected_error: str | None = None


class Exercise(Base):
    title: str
    # Markdown, rendered when it is shown; hints too.
    statement: str
    starter: str
    solution: str | None
    hints: list[str]
    rules: Rules
    time_s: float
    tests: list[ExerciseTest]
    # The file line of its `# Title`.
    line: int


class ExerciseDocument(Base):
    # A `Literal`, so that a reader handed a document of a format it does not
    # know fails here rather than misreading it.
    format: Literal[1] = EXERCISE_FORMAT
    # Always "python" for now: the harness is Python's. The field exists so a
    # second language is a new value rather than a new format.
    language: str = "python"
    title: str
    description: str
    # Markdown shown before the first exercise.
    intro: str
    exercises: list[Exercise]


class HarnessException(Base):
    # `mro` on the wire, `classes` here: a field called `mro` would shadow the
    # method every class has, and Pydantic warns about exactly that.
    model_config = ConfigDict(
        from_attributes=True, validate_by_name=True, serialize_by_alias=True
    )

    type: str
    # Class names, the exception's own first and `object` last: what an
    # `error: ValueError` test is matched against, subclasses included.
    classes: list[str] = Field(alias="mro")
    line: int | None
    traceback: str


class HarnessReport(Base):
    """The one JSON object `cocco/exercises/harness.py` writes on stdout.

    Pinned here, beside the document, for the judge that reads it; the harness
    itself cannot import it, being a stdlib program that runs inside a box.
    """

    stdout: str
    stderr: str
    truncated: bool
    exit: int
    exception: HarnessException | None
