import argparse
import asyncio
import logging
import os
import shutil
import tempfile
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from cocco import repository as repo
from cocco.config import config_path, load_config
from cocco.evaluators import DEFAULT_EVALUATOR, bundled_path
from cocco.schema import (
    Challenge,
    EvaluatorResult,
    Submission,
    SubmissionRunData,
    TestCase,
    TestOutcome,
)
from cocco.model import (
    metadata,
    OutcomeEnum,
)
from cocco.storage import Storage


logger = logging.getLogger(__name__)


# this is the length we truncate the output at, when saving
# it to the database. This prevents attacks that flood the
# database with data
MAX_OUTPUT_LENGTH = 80000


class BoxPool:
    """A pool of ids to be used with isolate boxes"""

    def __init__(self, num_boxes: int = 32):
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(num_boxes):
            self._queue.put_nowait(i)

    @asynccontextmanager
    async def acquire(self):
        box_id = await self._queue.get()
        try:
            yield box_id
        finally:
            self._queue.put_nowait(box_id)


_IDS_POOL = BoxPool()


# `nix build` on an already-realised store path still costs tens of
# milliseconds of process startup and evaluation, and `Language.run` resolves
# its runtime on *every* invocation - now twice per test case, once for the
# submission and once for the evaluator. The store path for a given flake
# output cannot change while the process lives, so remember it.
_RUNTIME_CACHE: Dict[tuple[str, str], Path] = {}


async def resolve_runtime(runtime_name: str, bin_name: str) -> Path:
    cached = _RUNTIME_CACHE.get((runtime_name, bin_name))
    if cached is not None:
        return cached
    logger.debug("resolving runtime %s for binary %s", runtime_name, bin_name)
    p = await asyncio.create_subprocess_shell(
        " ".join(
            ["nix", "build", f".#{runtime_name}", "--no-link", "--print-out-paths"]
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await p.communicate()
    if p.returncode != 0:
        logger.error(
            "nix build of %s failed (rc=%s): %s",
            runtime_name,
            p.returncode,
            stderr.decode().strip(),
        )
    assert p.returncode == 0
    nix_store_paths: List[str] = stdout.strip().decode().splitlines()
    for path in nix_store_paths:
        binary = Path(f"{path}/bin/{bin_name}")
        if binary.is_file():
            logger.debug("resolved %s -> %s", bin_name, binary)
            _RUNTIME_CACHE[(runtime_name, bin_name)] = binary
            return binary
    raise ValueError(f"could not find binary {bin_name} in {nix_store_paths}")


@dataclass(frozen=True)
class RunOutcome(object):
    retcode: int
    stdout: str
    stderr: str
    elapsed_time_s: float
    max_rss_kb: int
    status: str
    message: str | None
    oom: bool = False


def _classify_isolate_failure(res: RunOutcome) -> OutcomeEnum:
    # Caller guarantees res.status != "OK". OOM arrives as status=SG with
    # cg-oom-killed=1, so check oom before the SG branch.
    if res.status == "TO":
        return OutcomeEnum.timeout
    if res.oom:
        return OutcomeEnum.out_of_memory
    if res.status in ("SG", "RE"):
        return OutcomeEnum.runtime_error
    return OutcomeEnum.judge_internal_error


@dataclass(frozen=True)
class CompileOutcome(object):
    success: bool
    executable: Path | None
    stdout: str | None = None
    stderr: str | None = None


async def isolate_run(
    exe: Path,
    stdin: Path,
    args: List[str],
    time_limit: float = 2.0,
    mem_limit_kb: int = 512 * 1024,
    processes: int = 1,
    additional_files: List[Path] = [],
    box_files: Dict[str, Path] | None = None,
) -> RunOutcome:
    """Run `exe` in a fresh isolate box.

    `args` that happen to name an existing file or directory are copied into
    the box and rewritten to their *basename*: the box is one flat directory,
    so every path collapses into a single namespace. `box_files` is the
    explicit form of the same thing - a box-relative name to put a file under,
    whatever it is called outside - and is the only way to be sure two
    unrelated paths do not land on the same name.

    Ordering is load-bearing and easy to reverse by accident: `box_files` is
    copied first, then `args`, then `additional_files`, so a later stage
    silently wins a name collision with an earlier one.
    """
    box_files = box_files or {}
    async with _IDS_POOL.acquire() as box_id:
        logger.debug(
            "isolate run on box %s: exe=%s args=%s time=%.2fs mem=%dKB",
            box_id,
            exe,
            args,
            time_limit,
            mem_limit_kb,
        )
        isolate_init = await asyncio.create_subprocess_exec(
            "isolate",
            "--cg",
            "--box-id",
            str(box_id),
            "--init",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await isolate_init.communicate()
        if isolate_init.returncode != 0:
            raise RuntimeError(
                f"isolate --init failed (rc={isolate_init.returncode}): {stderr.decode()}"
            )
        box_path = Path(stdout.decode().strip()) / "box"

        for box_name, source in box_files.items():
            if os.path.isdir(source):
                shutil.copytree(source, box_path / box_name)
            else:
                shutil.copy(source, box_path / box_name)

        filtered_args = []
        for arg in args:
            # A name we placed ourselves is already in the box under exactly
            # that name; the `isfile` heuristic below must not touch it.
            if arg in box_files:
                filtered_args.append(arg)
            elif os.path.isfile(arg):
                arg = Path(arg)
                shutil.copy(arg, box_path / arg.name)
                filtered_args.append(arg.name)
            elif os.path.isdir(arg):
                arg = Path(arg)
                shutil.copytree(arg, box_path / arg.name)
                filtered_args.append(arg.name)
            else:
                filtered_args.append(arg)

        for f in additional_files:
            if os.path.isfile(f):
                shutil.copy(f, box_path / f.name)
            elif os.path.isdir(f):
                shutil.copytree(f, box_path / f.name)

        stdout_file = box_path / "_stdout"
        stderr_file = box_path / "_stderr"
        meta_file = f"/tmp/meta-{box_id}.txt"
        with open(stdin) as fp:
            isolate_run = await asyncio.create_subprocess_exec(
                "isolate",
                "--cg",
                "--box-id",
                str(box_id),
                f"--time={time_limit}",
                f"--cg-mem={mem_limit_kb}",
                f"--processes={processes}",
                "--dir=/nix/store",  # this allows running the compilers/interpreters
                f"--meta={meta_file}",
                f"--stdout={stdout_file.name}",
                f"--stderr={stderr_file.name}",
                "--run",
                "--",
                str(exe),
                *filtered_args,
                stdin=fp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await isolate_run.communicate()

        retcode = isolate_run.returncode
        # Submitted programs may emit non-UTF-8 bytes; decode permissively
        # rather than raising UnicodeDecodeError.
        with open(stdout_file, "rb") as fp:
            stdout = fp.read().decode("utf-8", errors="replace")
        with open(stderr_file, "rb") as fp:
            stderr = fp.read().decode("utf-8", errors="replace")

        with open(meta_file) as fp:
            meta = dict(line.strip().split(":", maxsplit=1) for line in fp.readlines())

        ret = RunOutcome(
            retcode=retcode or 127,
            stdout=stdout,
            stderr=stderr,
            elapsed_time_s=float(meta["time"]),
            max_rss_kb=int(meta["max-rss"]),
            status=meta.get("status", "OK"),
            message=meta.get("message", None),
            oom=meta.get("cg-oom-killed", "0").strip() == "1",
        )
        logger.debug(
            "isolate run on box %s done: status=%s rc=%s time=%.3fs rss=%dKB",
            box_id,
            ret.status,
            ret.retcode,
            ret.elapsed_time_s,
            ret.max_rss_kb,
        )

        isolate_cleanup = await asyncio.create_subprocess_exec(
            "isolate",
            "--cg",
            "--box-id",
            str(box_id),
            "--cleanup",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await isolate_cleanup.communicate()
        if isolate_cleanup.returncode != 0:
            raise RuntimeError(
                f"isolate --cleanup failed (rc={isolate_cleanup.returncode}):\n"
                f"{stdout.decode()}\n{stderr.decode()}"
            )

        return ret


@dataclass
class Language:
    """A language runtime loaded from `languages.toml`.

    Three cases, driven by `compile_args` and `run_binary`:
    - Interpreted (python): `compile_args` is None. `binary` is the interpreter;
      `run` invokes it with the source as the first argument.
    - Compiled native (cpp): `compile_args` is set. `binary` is the compiler;
      `run` executes the compiled output directly.
    - Compiled + VM (java): `compile_args` and `run_binary` are both set.
      `binary` is the compiler, `run_binary` is the VM used at execution time
      with args from `run_args`.

    Compile-time placeholders: `{source}`, `{output}`, `{workdir}`.
    Run-time placeholders: `{executable}`, `{workdir}`, `{name}`.
    """

    name: str
    extensions: List[str]
    runtime: str
    compiler: str | None = None
    compiler_args: List[str] = field(default_factory=list)
    output_ext: str = ""
    interpreter: str | None = None
    interpreter_args: List[str] = field(default_factory=list)
    additional_processes: int = 0

    @property
    def is_interpreted(self) -> bool:
        return self.interpreter is not None

    async def compile(self, source: Path, workdir: Path) -> CompileOutcome:
        if self.compiler is None:
            logger.debug("language %s is interpreted, skipping compilation", self.name)
            return CompileOutcome(executable=source, success=True)
        compiler = await resolve_runtime(self.runtime, self.compiler)
        output = workdir / (source.stem + self.output_ext)
        fmt_args = [
            a.format(source=str(source), output=str(output), workdir=str(workdir))
            for a in self.compiler_args
        ]
        logger.info("compiling %s with %s", source, self.name)
        proc = await asyncio.create_subprocess_exec(
            str(compiler),
            *fmt_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.debug("compilation of %s succeeded -> %s", source, output)
            return CompileOutcome(
                executable=output,
                success=True,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
            )
        else:
            logger.info(
                "compilation of %s failed (rc=%s)", source, proc.returncode
            )
            return CompileOutcome(
                executable=None,
                success=False,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
            )

    async def run(
        self,
        executable: Path,
        stdin: Path,
        args: List[str] | None = None,
        time_limit: float = 2.0,
        mem_limit_kb: int = 262144,
        processes: int = 1,
        box_files: Dict[str, Path] | None = None,
    ) -> RunOutcome:
        args = args or []
        if self.is_interpreted:
            assert self.interpreter is not None
            interpreter = await resolve_runtime(self.runtime, self.interpreter)
            fmt = dict(
                executable=str(executable),
                workdir=str(executable.parent),
                name=executable.stem,
            )
            prefix_args = [a.format(**fmt) for a in (self.interpreter_args)]
            return await isolate_run(
                interpreter,
                stdin,
                [*prefix_args, *args],
                time_limit=time_limit,
                mem_limit_kb=mem_limit_kb,
                processes=processes + self.additional_processes,
                additional_files=[executable],
                box_files=box_files,
            )
        return await isolate_run(
            # For compiled executables, we strip the absolute path, which is instead needed
            # for the interpreters above
            Path(executable.name),
            stdin,
            args,
            time_limit=time_limit,
            mem_limit_kb=mem_limit_kb,
            processes=processes + self.additional_processes,
            additional_files=[executable],
            box_files=box_files,
        )


def load_languages(path: Path) -> Dict[str, Language]:
    with open(path, "rb") as fp:
        data = tomllib.load(fp)
    languages = {name: Language(name=name, **cfg) for name, cfg in data.items()}
    logger.debug("loaded %d languages from %s: %s", len(languages), path, list(languages))
    return languages


def extension_to_language(languages: Dict[str, Language]) -> Dict[str, str]:
    """Extension (no dot, lowercased) -> language name."""
    return {
        ext.lower(): name
        for name, language in languages.items()
        for ext in language.extensions
    }


def language_for_filename(
    filename: str, languages: Dict[str, Language]
) -> Language | None:
    """The language a file name implies, or None if the extension is unknown.

    The same rule for a submitted solution and for an uploaded evaluator: the
    extension is the only thing that says what a file is, which is why neither
    carries a language field.
    """
    if "." not in filename:
        return None
    extension = filename.rsplit(".", 1)[-1].lower()
    name = extension_to_language(languages).get(extension)
    return languages[name] if name is not None else None


# An evaluator is course staff's code checking a whole output, not a student's
# solving one instance: a line diff of a multi-megabyte expected output is
# legitimately far more work than producing it was. Its budget is therefore
# fixed here rather than taken from the challenge - a teacher must not have to
# widen the *students'* limits to get their checker to finish.
EVALUATOR_TIME_LIMIT_S = 10.0
EVALUATOR_MEM_LIMIT_KB = 1024 * 1024  # 1 GB

# The names the evaluator's three arguments take inside the box. The leading
# dot is what makes them safe: `safe_filename` strips leading dots, so no
# uploaded file - test case or evaluator - can be named this, and no compiled
# binary named after one can either.
_ACTUAL_IN_BOX = ".actual_output"
_EXPECTED_IN_BOX = ".expected_output"
_INPUT_IN_BOX = ".test_input"

# The evaluator reads its three files by path; stdin is deliberately empty, so
# that "what does my checker read?" has one answer.
_DEV_NULL = Path("/dev/null")

# A compiler's complaint about the *evaluator* is emitted against every test
# case, and `visible_outcomes` only redacts hidden ones - so on a public sample
# this reaches students. Enough to identify the problem, not enough to paste
# the checker's source into the page.
MAX_EVALUATOR_COMPILE_OUTPUT = 2000


class EvaluatorError(Exception):
    """The challenge's evaluator is unusable: missing, in no known language, or
    it did not compile. Nothing to do with the submission, and it fails every
    test case rather than any one of them."""


@dataclass(frozen=True)
class Evaluator:
    """A challenge's checker, compiled once per submission and run once per
    test case."""

    language: Language
    executable: Path
    source_name: str


@dataclass(frozen=True)
class Evaluation:
    score: float
    message: str
    outcome: OutcomeEnum


def parse_evaluator_output(stdout: str) -> EvaluatorResult:
    """Parse the JSON object an evaluator printed, or raise `ValueError`."""
    try:
        return EvaluatorResult.model_validate_json(stdout)
    except ValidationError as e:
        raise ValueError(str(e)) from e


def _describe_evaluator_failure(res: RunOutcome) -> str:
    """Why an evaluator run did not produce a usable result.

    Deliberately not `_classify_isolate_failure`: that turns a timeout into
    `OutcomeEnum.timeout`, which is a verdict on the *student's* program. A
    checker that runs too long says nothing about the submission, and letting
    it masquerade as one would also put a bogus row on the `running_time`
    leaderboard, which admits only submissions that passed every test.
    """
    if res.status == "TO":
        return f"The evaluator timed out after {EVALUATOR_TIME_LIMIT_S:g} s."
    if res.oom:
        return (
            "The evaluator ran out of memory (limit "
            f"{EVALUATOR_MEM_LIMIT_KB // 1024} MB)."
        )
    detail = res.stderr.strip()[-MAX_EVALUATOR_COMPILE_OUTPUT:]
    return (
        f"The evaluator exited with status {res.status} (rc={res.retcode})."
        + (f"\n{detail}" if detail else "")
    )


async def prepare_evaluator(
    storage: Storage,
    evaluator_path: str | None,
    languages: Dict[str, Language],
    workdir: Path,
) -> Evaluator:
    """Resolve and compile a challenge's evaluator. Raises `EvaluatorError`.

    A NULL `evaluator_path` means the bundled default, which lives in the
    source tree and not in storage - so a fix to it reaches every challenge
    that never chose a checker of its own.
    """
    if evaluator_path is None:
        source = bundled_path(DEFAULT_EVALUATOR)
    else:
        source = storage.resolve(evaluator_path)
        if not source.is_file():
            raise EvaluatorError(
                f"The evaluator script {evaluator_path} is missing from storage."
            )

    language = language_for_filename(source.name, languages)
    if language is None:
        known = ", ".join(sorted("." + e for e in extension_to_language(languages)))
        raise EvaluatorError(
            f"The evaluator {source.name} is in no language this judge knows. "
            f"Accepted extensions: {known}."
        )

    # Under its own name, not a generated one: `javac` requires `Foo.java` for
    # `public class Foo`, and the extension is what tells a compiler what it is
    # being handed.
    local = workdir / source.name
    shutil.copy(source, local)

    compiled = await language.compile(local, workdir)
    if not compiled.success or compiled.executable is None:
        output = "\n".join(
            part for part in (compiled.stdout, compiled.stderr) if part
        ).strip()
        raise EvaluatorError(
            f"The evaluator {source.name} failed to compile:\n"
            + output[:MAX_EVALUATOR_COMPILE_OUTPUT]
        )

    logger.debug("prepared evaluator %s (%s)", source.name, language.name)
    return Evaluator(
        language=language, executable=compiled.executable, source_name=source.name
    )


async def evaluate(
    evaluator: Evaluator,
    actual_output: str,
    expected_output_path: Path,
    test_input_path: Path,
    workdir: Path,
) -> Evaluation:
    """Score one test case by running the challenge's evaluator on it.

    Never raises for evaluator misbehaviour: a checker that crashes, hangs or
    prints nonsense is a normal per-test result, reported as `evaluator_error`
    with a message course staff can act on.
    """
    # The submission's output arrives as a str - isolate's box, and the file it
    # was captured from, are long gone by now - so it is the one of the three
    # that has to be put back on disk.
    actual_path = workdir / _ACTUAL_IN_BOX
    actual_path.write_text(actual_output, encoding="utf-8")

    res = await evaluator.language.run(
        evaluator.executable,
        _DEV_NULL,
        args=[_ACTUAL_IN_BOX, _EXPECTED_IN_BOX, _INPUT_IN_BOX],
        time_limit=EVALUATOR_TIME_LIMIT_S,
        mem_limit_kb=EVALUATOR_MEM_LIMIT_KB,
        box_files={
            _ACTUAL_IN_BOX: actual_path,
            _EXPECTED_IN_BOX: expected_output_path,
            _INPUT_IN_BOX: test_input_path,
        },
    )

    if res.status != "OK":
        return Evaluation(
            score=0.0,
            message=_describe_evaluator_failure(res),
            outcome=OutcomeEnum.evaluator_error,
        )

    try:
        result = parse_evaluator_output(res.stdout)
    except ValueError as e:
        logger.info(
            "evaluator %s printed unusable output: %s", evaluator.source_name, e
        )
        return Evaluation(
            score=0.0,
            message=(
                "The evaluator did not print a valid result "
                '(expected one JSON object, e.g. {"score": 1.0}). '
                f"It printed:\n{res.stdout[:1000]}"
            ),
            outcome=OutcomeEnum.evaluator_error,
        )

    return Evaluation(
        score=result.score, message=result.message, outcome=OutcomeEnum.success
    )


def _every_test(
    run_data: SubmissionRunData, outcome: OutcomeEnum, message: str
) -> List[TestOutcome]:
    """The same verdict on every test case, for a failure that is not about any
    one of them: the submission did not compile, or the evaluator is broken."""
    return [
        TestOutcome(
            submission_id=run_data.submission_id,
            testcase_id=test_case.id,
            outcome=outcome,
            score=0.0,
            message=message[:MAX_OUTPUT_LENGTH],
        )
        for test_case in run_data.test_cases
    ]


async def run_submission(
    storage: Storage,
    run_data: SubmissionRunData,
) -> List[TestOutcome]:
    logger.info(
        "running submission %s (language=%s, tests=%d)",
        run_data.submission_id,
        run_data.language,
        len(run_data.test_cases),
    )
    languages = load_languages(Path("languages.toml"))
    runtime: Language = languages[run_data.language]

    outcomes = []
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        # The dot matters: a compiler is told what it has been handed by the
        # extension, and `languages.toml` spells extensions without one.
        source_fname = temp / ("source." + runtime.extensions[0])
        with open(source_fname, "w") as fp:
            fp.write(run_data.source)

        # First we compile
        compile_result = await runtime.compile(source_fname, temp)
        if not compile_result.success:
            logger.info(
                "submission %s failed to compile, marking all %d tests as compile_failure",
                run_data.submission_id,
                len(run_data.test_cases),
            )
            # The evaluator is deliberately not prepared here: there is no
            # output to check, so a broken checker is not this run's problem.
            return _every_test(
                run_data,
                OutcomeEnum.compile_failure,
                f"stdout:\n{compile_result.stdout}\n\nstderr:\n{compile_result.stderr}",
            )
        assert compile_result.executable is not None

        # Once per submission, not once per test case, and after the
        # submission compiled so a broken checker cannot mask a broken
        # solution.
        evaluator_dir = temp / "evaluator"
        evaluator_dir.mkdir()
        try:
            evaluator = await prepare_evaluator(
                storage, run_data.evaluator_path, languages, evaluator_dir
            )
        except EvaluatorError as e:
            logger.error(
                "submission %s: challenge evaluator is unusable: %s",
                run_data.submission_id,
                e,
            )
            return _every_test(run_data, OutcomeEnum.evaluator_error, str(e))

        for test_case in run_data.test_cases:
            try:
                exec_res = await runtime.run(
                    compile_result.executable,
                    storage.resolve(test_case.input_path),
                    [],
                    time_limit=run_data.max_time_s,
                    mem_limit_kb=run_data.max_memory_bytes // 1024,
                    processes=run_data.max_threads,
                )
                if exec_res.status != "OK":
                    logger.info(
                        "submission %s test %s failed: status=%s message=%s",
                        run_data.submission_id,
                        test_case.id,
                        exec_res.status,
                        exec_res.message,
                    )
                    score = 0.0
                    eval_msg = exec_res.message or exec_res.status
                    outcome = _classify_isolate_failure(exec_res)
                else:
                    # The evaluator sees the whole output; only what is stored
                    # on the outcome below is truncated.
                    evaluation = await evaluate(
                        evaluator,
                        exec_res.stdout,
                        storage.resolve(test_case.expected_output_path),
                        storage.resolve(test_case.input_path),
                        evaluator_dir,
                    )
                    score = evaluation.score
                    eval_msg = evaluation.message
                    outcome = evaluation.outcome
                    logger.debug(
                        "submission %s test %s scored %.3f (%s)",
                        run_data.submission_id,
                        test_case.id,
                        score,
                        outcome.value,
                    )
                test_outcome = TestOutcome(
                    submission_id=run_data.submission_id,
                    testcase_id=test_case.id,
                    outcome=outcome,
                    score=score,
                    # The submission's own time and memory, never the
                    # evaluator's: these are what the running_time leaderboard
                    # ranks by.
                    elapsed_s=exec_res.elapsed_time_s,
                    peak_memory_bytes=exec_res.max_rss_kb * 1024,
                    # Truncated like the outputs: the message now comes from a
                    # teacher's script running with a gigabyte to play with,
                    # and the column it lands in has no length limit.
                    message=eval_msg[:MAX_OUTPUT_LENGTH],
                    # outputs are truncated to prevent flooding attacks
                    stdout=exec_res.stdout[:MAX_OUTPUT_LENGTH],
                    stderr=exec_res.stderr[:MAX_OUTPUT_LENGTH],
                )
            except Exception as e:
                logger.exception(
                    "submission %s test %s: judge internal error",
                    run_data.submission_id,
                    test_case.id,
                )
                test_outcome = TestOutcome(
                    submission_id=run_data.submission_id,
                    testcase_id=test_case.id,
                    outcome=OutcomeEnum.judge_internal_error,
                    score=0.0,
                    message=f"Judge internal error: {e!r}",
                )
            outcomes.append(test_outcome)

    return outcomes


async def run_from_queue(engine: Engine, storage: Storage) -> bool:
    # `connect`, not `begin`: pull_queued_submission commits by itself as soon
    # as it has claimed the submission, so that the row lock is not held while
    # the challenge and its test cases are read.
    with engine.connect() as conn:
        run_data = await repo.pull_queued_submission(conn)
        if run_data is None:
            return False

    logger.info("picked up submission %s from queue", run_data.submission_id)

    outcomes: List[TestOutcome] = []
    try:
        # `languages.toml` is deliberately not configurable, unlike the
        # database and the storage root: a language entry names a runtime
        # `flake.nix` must expose, and `resolve_runtime` builds it out of *this*
        # checkout, so a file pointed elsewhere would advertise a freedom that
        # does not exist - and be one more way for the two processes to
        # disagree in silence.
        outcomes = await run_submission(storage, run_data)
    except Exception as e:
        logger.exception(
            "submission %s failed during execution; marking as judge_internal_error",
            run_data.submission_id,
        )
        message = f"Judge internal error: {e!r}"
        outcomes = [
            TestOutcome(
                submission_id=run_data.submission_id,
                testcase_id=t.id,
                outcome=OutcomeEnum.judge_internal_error,
                score=0.0,
                message=message,
            )
            for t in run_data.test_cases
        ]

    with engine.begin() as conn:
        repo.insert_test_outcomes(conn, outcomes)
        repo.mark_submission_done(conn, run_data.submission_id)
    logger.info(
        "submission %s done, recorded %d test outcomes",
        run_data.submission_id,
        len(outcomes),
    )

    return True


async def run_loop(engine: Engine, storage: Storage, wait_time=5):
    logger.info("judge run loop started (poll interval %ss)", wait_time)
    while True:
        has_run = await run_from_queue(engine, storage)
        if not has_run:
            logger.debug("judge waiting for %f seconds", wait_time)
            await asyncio.sleep(wait_time)


if __name__ == "__main__":
    async def main():
        parser = argparse.ArgumentParser(
            description="Run the cocco judge loop, picking up queued submissions."
        )
        # The database and the storage root are not flags. They are the two
        # things this process must agree with the web app about, and a flag
        # that can disagree with the file is the same bug as an environment
        # variable that can - so there is one way to say them, and it is the
        # file they both read.
        parser.add_argument(
            "--config",
            type=Path,
            default=config_path(),
            help=(
                "Path to the configuration file, which is where the database "
                "and the storage root come from (default: $COCCO_CONFIG, else "
                "cocco.toml)"
            ),
        )
        parser.add_argument(
            "--log-level",
            default="INFO",
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            help="Logging verbosity (default: INFO)",
        )
        args = parser.parse_args()

        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        # Read after `basicConfig`, so that what `load_config` has to say -
        # a missing file, a setting in its old place, no session secret -
        # actually lands in the judge's log rather than nowhere.
        config = load_config(args.config)

        storage = Storage(config.storage.root)
        engine = create_engine(config.database.url)
        metadata.create_all(engine)
        await run_loop(engine, storage)

    asyncio.run(main())
