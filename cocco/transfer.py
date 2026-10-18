"""The challenge transfer format: what a challenge looks like as a directory.

A challenge is otherwise only reachable one HTTP form at a time - one upload per
test case, one arrow-press per reorder, a `<textarea>` for the statement. This
module is the other way in: it says how a challenge is spelled as files, so it
can be pulled, edited with an editor, and pushed back.

Nothing here touches FastAPI, the database or `Storage` - it takes and returns
plain data, which is the seam that makes the *format* testable by import rather
than only through a round trip. `main.py` owns the I/O; this owns the shape.

The zip is flat, and mirrors what is already on disk. A challenge's test files
live in one directory with no per-test subdirectory (`_testcase_dir` in
`main.py`), which is why an input and an output name are already required to be
unique within a challenge; `tests/` keeps exactly that shape.

    challenge.toml     the configuration - this module
    README.md          challenge.description, verbatim
    tests/01.in        the names are the identity - see `plan_testcases`
    tests/01.out
    evaluator/check.py only when the challenge has an uploaded evaluator
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import tomllib

import toml

from cocco.model import LeaderboardRanking
from cocco.storage import safe_filename

MANIFEST_NAME = "challenge.toml"
STATEMENT_NAME = "README.md"
TESTS_DIR = "tests"
EVALUATOR_DIR = "evaluator"

# An exercise set is one file, the source an import stores verbatim, so it has
# no format of its own here: a pulled set is a directory holding this file and
# nothing else. Its name is fixed, as `challenge.toml`'s is, so that a directory
# says what it holds; the set's title is in the file's front matter.
EXERCISES_NAME = "exercises.md"

HEADER = """\
# A cocco challenge. Edit these files, then push them back from this directory:
#
#     cocco push
#
# (or `cocco push {challenge_id} <directory>` from anywhere else). If the
# challenge was changed on the web in the meantime, the push is refused until
# `cocco pull` has brought those changes in.
#
# The problem statement is {statement}, in markdown, and the test data is in
# {tests}/. The order of the [[testcase]] blocks below is the order the judge
# runs them in - move a block to reorder. A test case is identified by its
# input file name: rename that and you have replaced it, not renamed it.
#
# Removing a block retires the test case if anything has ever been graded on
# it, and deletes it otherwise. Nothing here can strand a score.
"""


class ManifestError(ValueError):
    """A manifest that cannot be applied, with the sentence to show for it.

    A `ValueError` because that is what a bad value is, and one exception for
    every way the file can be wrong because there is one place it is reported:
    the import route turns it into a 400 and the CLI prints it.
    """


@dataclass(frozen=True)
class ManifestTestCase:
    """One `[[testcase]]` block."""

    input: str
    expected_output: str
    is_sample: bool = False
    is_active: bool = True

    @property
    def input_name(self) -> str:
        return self.input.split("/")[-1]

    @property
    def expected_output_name(self) -> str:
        return self.expected_output.split("/")[-1]


@dataclass(frozen=True)
class Manifest:
    """`challenge.toml`, parsed.

    Three things are deliberately *not* fields here. `position` - where the
    challenge sits on its course page - belongs to the course editor, and a key
    that silently did nothing would be worse than no key. `course_id` and
    `challenge_id`, because the challenge a push lands on is the one named in
    the URL, not a number inside a file somebody may have copied. And
    `execution_order`, because the order of the blocks *is* the order, which is
    the whole point of editing this in a text editor.

    `visible_from` is not one of them, and the difference is worth stating: it
    is a column a challenge form writes, so `_challenge_fields` validates it for
    a push exactly as it does for the editor. Absent means hidden - which is a
    real value and not a missing one - so a pulled hidden challenge has no key
    and pushing that file back hides it, which round-trips. TOML has native
    datetimes, so it is written offset-carrying; one typed by hand without an
    offset is read on the server's clock, the same rule the form's box uses.
    """

    title: str
    ranking: LeaderboardRanking
    max_time_s: float
    max_memory_mb: float
    max_threads: int
    evaluator: str | None
    testcases: tuple[ManifestTestCase, ...]
    visible_from: datetime | None = None

    @property
    def evaluator_name(self) -> str | None:
        return None if self.evaluator is None else self.evaluator.split("/")[-1]


@dataclass(frozen=True)
class ExistingTestCase:
    """A test case as the database has it, reduced to what a plan needs.

    A dataclass rather than the SQLAlchemy Row `query_challenge_testcases`
    returns, so that `plan_testcases` - which is where the rule about graded
    test cases lives - can be tested without a database.
    """

    id: int
    input_name: str
    expected_output_name: str
    is_sample: bool
    is_active: bool
    n_outcomes: int


@dataclass(frozen=True)
class Update:
    """A test case the manifest still names: same input file, new everything else."""

    existing: ExistingTestCase
    entry: ManifestTestCase
    execution_order: int


@dataclass(frozen=True)
class Addition:
    entry: ManifestTestCase
    execution_order: int


@dataclass(frozen=True)
class Plan:
    """What a push does to a challenge's test cases, decided before anything is
    written. `retired` and `deleted` are the same absence with different
    histories - see `plan_testcases`."""

    updated: tuple[Update, ...]
    added: tuple[Addition, ...]
    retired: tuple[ExistingTestCase, ...]
    deleted: tuple[ExistingTestCase, ...]


# ---------------------------------------------------------------------------
# Writing


def dump_manifest(manifest: Manifest, header: str = "") -> str:
    """The manifest as the text that goes into `challenge.toml`.

    Written with `toml.dumps` rather than by hand: a title with a quote or a
    newline in it has to survive the round trip, and hand-rolled escaping is
    exactly the kind of thing that works until somebody names a challenge
    "Dijkstra's". The header is prepended as comments, which `dumps` has no way
    to emit and `tomllib` ignores on the way back in.
    """
    document: dict = {
        "title": manifest.title,
        "ranking": manifest.ranking.value,
        "max_time_s": manifest.max_time_s,
        "max_memory_mb": manifest.max_memory_mb,
        "max_threads": manifest.max_threads,
    }
    if manifest.visible_from is not None:
        document["visible_from"] = manifest.visible_from
    if manifest.evaluator is not None:
        document["evaluator"] = manifest.evaluator
    document["testcase"] = [
        {
            "input": tc.input,
            "expected_output": tc.expected_output,
            "is_sample": tc.is_sample,
            "is_active": tc.is_active,
        }
        for tc in manifest.testcases
    ]
    # `dumps` runs the tables together; one blank line before each makes the
    # blocks something you can move around without counting lines.
    body = toml.dumps(document).replace("[[testcase]]", "\n[[testcase]]")
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return f"{header}\n{body}" if header else body


def challenge_version(
    manifest: Manifest, statement: str, files: Iterable[tuple[str, int, int]]
) -> str:
    """A digest of everything an export carries: the version a pull records and
    a push's `If-Match` names.

    `files` is `(name in the archive, size, mtime in ns)` per stored file rather
    than the file's bytes - `_challenge_version` in `main.py` says why. The
    manifest is hashed without its header, so rewording the comment at the top
    of `challenge.toml` does not make every checkout look stale. Each part is
    length-prefixed, so no statement can be spelled to look like a file entry.
    """
    digest = hashlib.sha256()
    for part in (dump_manifest(manifest), statement):
        data = part.encode("utf-8")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    for name, size, mtime_ns in sorted(files):
        data = name.encode("utf-8")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
        digest.update(f"{size}:{mtime_ns};".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Reading


def _require(table: dict, key: str, kinds: tuple[type, ...], where: str) -> Any:
    if key not in table:
        raise ManifestError(f"{where} is missing `{key}`.")
    value = table[key]
    # `bool` is a subclass of `int`, so a `true` where a number belongs would
    # otherwise pass as 1 and quietly become a limit.
    if isinstance(value, bool) and bool not in kinds:
        raise ManifestError(f"{where}: `{key}` must be a number, not true/false.")
    if not isinstance(value, kinds):
        names = " or ".join(k.__name__ for k in kinds)
        raise ManifestError(f"{where}: `{key}` must be {names}.")
    return value


def _member_path(value: str, directory: str, where: str) -> str:
    """Check one file reference, and return it in its canonical `dir/name` form.

    Every path a manifest names has to be a bare file directly under its
    directory. A `../`, an absolute path, a backslash or a nested
    `tests/a/b.in` is refused *here*, before anything is opened -
    `Storage.resolve` is the backstop for a mistake, not the check.

    `safe_filename` is the same reduction every upload in the app goes through,
    and it has to be a no-op on the name given: a manifest naming `a b.in` and
    a zip containing `a b.in` would otherwise be silently stored as `a_b.in`,
    which is a file the next pull would not produce.
    """
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}: expected a file name.")
    prefix = f"{directory}/"
    if not value.startswith(prefix):
        raise ManifestError(f"{where}: {value!r} must be inside {prefix}")
    name = value[len(prefix):]
    if "/" in name or "\\" in name:
        raise ManifestError(
            f"{where}: {value!r} - files go directly in {prefix}, not in a "
            "subdirectory."
        )
    try:
        if safe_filename(name) != name:
            raise ManifestError(
                f"{where}: {name!r} is not a usable file name. Use letters, "
                "digits, dot, dash and underscore."
            )
    except ValueError as e:
        raise ManifestError(f"{where}: {name!r} - {e}") from e
    return f"{prefix}{name}"


def parse_manifest(text: str) -> Manifest:
    """`challenge.toml` as a `Manifest`, or `ManifestError` saying why not.

    Structure and file names are checked here. The sandbox limits are *not*:
    `_challenge_fields` in `main.py` is already the one place the create form
    and the edit form agree on what a valid limit is, and the import route is
    its third caller rather than a second opinion.
    """
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{MANIFEST_NAME} is not valid TOML: {e}") from e

    title = _require(table, "title", (str,), MANIFEST_NAME).strip()
    if not title:
        raise ManifestError("A challenge needs a title.")

    ranking_name = _require(table, "ranking", (str,), MANIFEST_NAME)
    try:
        ranking = LeaderboardRanking(ranking_name)
    except ValueError as e:
        known = ", ".join(r.value for r in LeaderboardRanking)
        raise ManifestError(
            f"Unknown ranking {ranking_name!r}. One of: {known}."
        ) from e

    max_time_s = float(_require(table, "max_time_s", (int, float), MANIFEST_NAME))
    max_memory_mb = float(
        _require(table, "max_memory_mb", (int, float), MANIFEST_NAME)
    )
    max_threads = _require(table, "max_threads", (int,), MANIFEST_NAME)

    # Read through and not validated, exactly as the limits above are: whether
    # a datetime is one `_challenge_fields` will accept is that function's
    # business, and this is not a second opinion. The type *is* checked, because
    # `visible_from = "soon"` is a TOML string and would otherwise arrive at the
    # database as one.
    visible_from = table.get("visible_from")
    if visible_from is not None and not isinstance(visible_from, datetime):
        raise ManifestError(
            "`visible_from` must be a date and time, e.g. "
            "2026-09-01T09:00:00+02:00. Leave it out to hide the challenge."
        )

    evaluator = table.get("evaluator")
    if evaluator is not None:
        evaluator = _member_path(evaluator, EVALUATOR_DIR, "`evaluator`")

    raw_testcases = table.get("testcase", [])
    if not isinstance(raw_testcases, list):
        raise ManifestError("`testcase` must be a list of [[testcase]] blocks.")

    testcases: list[ManifestTestCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_testcases, start=1):
        where = f"[[testcase]] {index}"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where} is not a table.")
        input_path = _member_path(
            _require(raw, "input", (str,), where), TESTS_DIR, where
        )
        output_path = _member_path(
            _require(raw, "expected_output", (str,), where), TESTS_DIR, where
        )
        if input_path == output_path:
            raise ManifestError(
                f"{where}: the input and the expected output must be different "
                "files."
            )
        # Both files of every test case share one flat directory on disk, so a
        # name used twice anywhere in the challenge is one file standing in for
        # two - which is data loss, not a naming quibble.
        for path in (input_path, output_path):
            if path in seen:
                raise ManifestError(
                    f"{where}: {path} is used twice. Every file name in "
                    f"{TESTS_DIR}/ must be distinct."
                )
            seen.add(path)
        testcases.append(
            ManifestTestCase(
                input=input_path,
                expected_output=output_path,
                is_sample=bool(raw.get("is_sample", False)),
                is_active=bool(raw.get("is_active", True)),
            )
        )

    return Manifest(
        title=title,
        ranking=ranking,
        max_time_s=max_time_s,
        max_memory_mb=max_memory_mb,
        max_threads=max_threads,
        evaluator=evaluator,
        testcases=tuple(testcases),
        visible_from=visible_from,
    )


# ---------------------------------------------------------------------------
# Reconciling


def plan_testcases(
    existing: list[ExistingTestCase], manifest: Manifest
) -> Plan:
    """What a push does to the test cases already there.

    A test case is identified by its **input file name**. The manifest carries
    no ids on purpose - it is meant to be edited, moved between checkouts and
    kept in git - and the file name is the only handle a person editing a
    directory can keep stable. Renaming an input therefore replaces the test
    case rather than renaming it, which the header comment says out loud.

    The rule for a test case the manifest no longer names is the one
    `post_testcase_delete` already enforces, and it is why an import can never
    strand a score: if anything has been graded on it, it is **retired** -
    row, files and outcomes all kept, but no new submission runs it - and only
    a test case nothing was ever scored against is actually deleted.
    """
    by_name = {tc.input_name: tc for tc in existing}
    named = {tc.input_name for tc in manifest.testcases}

    updated: list[Update] = []
    added: list[Addition] = []
    for order, entry in enumerate(manifest.testcases):
        found = by_name.get(entry.input_name)
        if found is None:
            added.append(Addition(entry=entry, execution_order=order))
        else:
            updated.append(
                Update(existing=found, entry=entry, execution_order=order)
            )

    gone = [tc for tc in existing if tc.input_name not in named]
    return Plan(
        updated=tuple(updated),
        added=tuple(added),
        retired=tuple(tc for tc in gone if tc.n_outcomes),
        deleted=tuple(tc for tc in gone if not tc.n_outcomes),
    )
