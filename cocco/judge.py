import argparse
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import tomllib
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from cocco import repository as repo
from cocco.config import (
    DEFAULT_FLAKE_REF,
    ConfigError,
    config_path,
    default_gcroots,
    load_config,
    load_log_config,
)
from cocco.evaluators import DEFAULT_EVALUATOR, bundled_path
from cocco.logs import bind, configure_logging
from cocco.schema import (
    Challenge,
    EvaluatorResult,
    Submission,
    SubmissionRunData,
    TestCase,
    TestOutcome,
)
from cocco.migrate import SchemaOutOfDate, check_schema_is_current
from cocco.model import (
    OutcomeEnum,
)
from cocco.storage import (
    Storage,
    StorageFormatError,
    check_storage_format,
    expand,
    open_plaintext,
    safe_filename,
)


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


# Every runtime `languages.toml` names, resolved once by `prewarm_runtimes`
# before the judge grades anything and read from here forever after. The store
# path for a given flake output cannot change while the process lives.
#
# It used to fill *lazily*, on the first use of each language, which put a `nix
# build` in the middle of a graded run: `Language.run` resolves its runtime on
# every invocation - twice per test case, once for the submission and once for
# the evaluator - so the first java upload paid for a JDK, and a flake that
# could not build one said so then rather than at startup.
_RUNTIME_CACHE: Dict[tuple[str, str], Path] = {}

# Which flake the runtimes are built out of - `[judge] flake`, see `JudgeConfig`.
# A module-level name rather than an argument threaded through `Language.run`,
# `run_submission` and `run_loop`, for the reason `main.DUMMY_AUTH` is one:
# `Language` is a frozen dataclass built straight from `languages.toml` and has
# nowhere to carry it, and this is one value set once at startup and read from
# the bottom of the call stack. `main()` assigns it; tests monkeypatch it.
#
# `_RUNTIME_CACHE` is not keyed on it, and need not be: it is fixed for the
# life of the process.
FLAKE_REF = DEFAULT_FLAKE_REF

# Where the symlinks that root those runtimes are written - `[judge] gcroots`.
# The same seam as `FLAKE_REF`, assigned beside it by `main()`, and starting at
# the default rather than at None so that anything resolving a runtime without
# going through `main()` - the integration tests, notably - still puts its roots
# somewhere durable instead of failing on a missing directory.
GCROOTS_DIR = default_gcroots()


class RuntimeUnavailable(Exception):
    """The judge cannot get hold of a language runtime.

    One name for both ways that happens, because from the queue's point of view
    they are the same thing: `nix build` failed, or what it built does not
    contain the binary a language asks for. It belongs to startup, where it is
    fatal and names the language at fault - never to the middle of a grading
    run, where by then the cache has an answer or `languages.toml` disagrees
    with itself.
    """


async def build_runtime(runtime_name: str) -> List[Path]:
    """Build one flake output and return its store paths, *rooted*.

    `--out-link` is the whole point of this function. It is what makes nix
    register an indirect GC root under /nix/var/nix/gcroots/auto for the path it
    has just built. With the `--no-link` this used to pass, the store path was
    held by nothing but a `Path` in `_RUNTIME_CACHE` - which nix cannot see - so
    a `nix-store --gc` on the judge host was free to collect the very
    interpreter a running judge was about to bind-mount into a box, and the
    failure landed as submissions dying on a missing file with nothing in the
    log to say why.
    """
    GCROOTS_DIR.mkdir(parents=True, exist_ok=True)
    # The directory is resolved, and the runtime's name joined on afterwards -
    # never `(GCROOTS_DIR / runtime_name).resolve()`, which on the second run
    # would follow the link written by the first and hand `--out-link` a path
    # inside the store itself. Absolute because nix records an indirect root by
    # the canonical path of the link, so a relative one roots something that
    # depends on where the judge happened to be started.
    out_link = GCROOTS_DIR.resolve() / runtime_name
    # `exec`, not `shell`: the flake ref comes out of a configuration file now
    # rather than being a literal here, and joining it into a command line is
    # the one way that becomes interesting.
    p = await asyncio.create_subprocess_exec(
        "nix",
        "build",
        f"{FLAKE_REF}#{runtime_name}",
        "--out-link",
        str(out_link),
        "--print-out-paths",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await p.communicate()
    if p.returncode != 0:
        # Raised, where this used to `assert p.returncode == 0`: it is the
        # startup failure now, and an assertion vanishes under `python -O`.
        raise RuntimeUnavailable(
            f"nix build of {FLAKE_REF}#{runtime_name} failed "
            f"(rc={p.returncode}): {stderr.decode().strip()}"
        )
    return [Path(line) for line in stdout.strip().decode().splitlines()]


async def prewarm_runtimes(languages: Dict[str, "Language"]) -> None:
    """Build and root every runtime `languages.toml` names, once, at startup.

    Which is what `main()` has always said it was doing and never did. Three
    things follow from doing it here rather than on first use: every runtime
    gets its GC root whether or not anybody submits in that language, a flake
    that cannot build one is a judge that refuses to start rather than a java
    submission that fails hours later, and `nix` is never invoked at all while a
    submission is being graded.

    One build per *runtime*, not per binary: gcc answers for both `gcc` and
    `g++` and jdk for both `javac` and `java`, and building the same flake
    output twice would do nothing but write the same symlink again.
    """
    wanted: Dict[str, set[str]] = {}
    for language in languages.values():
        for bin_name in (language.compiler, language.interpreter):
            if bin_name is not None:
                wanted.setdefault(language.runtime, set()).add(bin_name)

    for runtime_name, binaries in sorted(wanted.items()):
        logger.info("building runtime %s from %s", runtime_name, FLAKE_REF)
        out_paths = await build_runtime(runtime_name)
        for bin_name in sorted(binaries):
            for out in out_paths:
                binary = out / "bin" / bin_name
                if binary.is_file():
                    logger.info("resolved %s -> %s", bin_name, binary)
                    _RUNTIME_CACHE[(runtime_name, bin_name)] = binary
                    break
            else:
                needed_by = sorted(
                    language.name
                    for language in languages.values()
                    if language.runtime == runtime_name
                    and bin_name in (language.compiler, language.interpreter)
                )
                raise RuntimeUnavailable(
                    f"{FLAKE_REF}#{runtime_name} exposes no bin/{bin_name}, "
                    f"which {', '.join(needed_by)} needs"
                )


async def resolve_runtime(runtime_name: str, bin_name: str) -> Path:
    """Where a runtime's binary is. A lookup, and nothing else.

    Still `async`, so `Language.compile` and `Language.run` are untouched, but
    it no longer builds anything: `prewarm_runtimes` has already filled the
    cache from the same `languages.toml` these two names come out of. A miss
    therefore means those two have disagreed, which is a bug to report and not
    a condition to paper over by building a compiler halfway through grading
    somebody's submission.
    """
    binary = _RUNTIME_CACHE.get((runtime_name, bin_name))
    if binary is None:
        raise RuntimeUnavailable(
            f"runtime {runtime_name} was never resolved for {bin_name}: "
            "prewarm_runtimes did not cover it"
        )
    return binary


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


async def _drain(stream: asyncio.StreamReader) -> None:
    """Read a pipe to EOF and throw it away. Isolate's own messages go here,
    and a pipe nobody empties is a process that never exits."""
    await stream.read()


async def _feed_stdin(writer: asyncio.StreamWriter, path: Path) -> None:
    """Decompress `path` into a running box's standard input.

    Storage is compressed and a box is plain, and this is the one crossing that
    costs no file: the input is inflated straight into the pipe isolate is
    reading, so a million-line test case is never written out whole. It used to
    be `open(stdin)` and the file's own descriptor, which only worked while the
    bytes on disk were the bytes the program should read.

    The chunk reads go through a thread because zlib releases the GIL and a
    multi-megabyte inflate on the event loop would stall every other box. A
    program that exits before reading all of its input is ordinary - that is
    what a broken pipe means here - and is not the submission's problem to
    report: the box's own status already says how it ended.
    """
    try:
        with open_plaintext(path) as fp:
            while chunk := await asyncio.to_thread(fp.read, 1 << 20):
                writer.write(chunk)
                await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with suppress(BrokenPipeError, ConnectionResetError):
            writer.close()


async def isolate_run(
    exe: Path,
    stdin: Path | None,
    args: List[str],
    time_limit: float = 2.0,
    mem_limit_kb: int = 512 * 1024,
    processes: int = 1,
    additional_files: List[Path] = [],
    box_files: Dict[str, Path] | None = None,
    compressed_box_files: Dict[str, Path] | None = None,
) -> RunOutcome:
    """Run `exe` in a fresh isolate box.

    `stdin` is `None` for an empty standard input, or a **compressed file in
    storage**, which is decompressed into the box's stdin as it runs. One rule
    with no flag to get wrong: everything under the storage root is gzip, and
    nothing else is ever passed here.

    `args` that happen to name an existing file or directory are copied into
    the box and rewritten to their *basename*: the box is one flat directory,
    so every path collapses into a single namespace. `box_files` is the
    explicit form of the same thing - a box-relative name to put a file under,
    whatever it is called outside - and is the only way to be sure two
    unrelated paths do not land on the same name. `compressed_box_files` is
    that same argument for a file that comes out of storage, and so has to be
    expanded rather than copied; the two are separate arguments rather than one
    clever value because a caller mixing them up would be silent.

    Ordering is load-bearing and easy to reverse by accident: `box_files` and
    `compressed_box_files` are placed first, then `args`, then
    `additional_files`, so a later stage silently wins a name collision with an
    earlier one.
    """
    box_files = box_files or {}
    compressed_box_files = compressed_box_files or {}
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

        for box_name, source in compressed_box_files.items():
            # One write, the same one `shutil.copy` above was already doing:
            # the file is inflated straight into the box rather than through a
            # plaintext copy somewhere else first. On a thread for
            # `_feed_stdin`'s reason.
            await asyncio.to_thread(expand, source, box_path / box_name)

        filtered_args = []
        for arg in args:
            # A name we placed ourselves is already in the box under exactly
            # that name; the `isfile` heuristic below must not touch it.
            if arg in box_files or arg in compressed_box_files:
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
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # This is `communicate()` written out, and it has to be: given a stdin
        # pipe, that method closes it before returning *whether or not it was
        # given anything to write*, so the box would see EOF while the feeder
        # was still inflating into it. The two output pipes still have to be
        # drained here rather than after the wait - isolate's own messages fill
        # them, and a full pipe is a process that never exits. Both are then
        # discarded: what the run printed is read back out of the box's own
        # `_stdout` and `_stderr` below.
        assert isolate_run.stdout is not None and isolate_run.stderr is not None
        pumps = [_drain(isolate_run.stdout), _drain(isolate_run.stderr)]
        if stdin is not None:
            assert isolate_run.stdin is not None
            pumps.append(_feed_stdin(isolate_run.stdin, stdin))
        await asyncio.gather(*pumps)
        await isolate_run.wait()

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

    def _run_artifacts(self, executable: Path) -> List[Path]:
        """Everything the run needs in the box beside the entry point.

        `javac` writes one `.class` per class, not one per file: a submission
        with an inner, an anonymous or a second top-level class compiles to
        `Main.class` *and* `Main$Helper.class`, and a box holding only the first
        dies with NoClassDefFoundError the moment it is used. So a compiled + VM
        language ships its whole output directory; an interpreted one has only
        the source file it was handed.

        Scoped by directory, which is what keeps it honest: a submission's
        classes are compiled into the run's temp dir and an evaluator's into
        `evaluator/` beneath it, so neither picks up the other's.
        """
        if self.compiler is None or not self.output_ext:
            return [executable]
        return sorted(executable.parent.glob("*" + self.output_ext)) or [executable]

    async def run(
        self,
        executable: Path,
        stdin: Path | None,
        args: List[str] | None = None,
        time_limit: float = 2.0,
        mem_limit_kb: int = 262144,
        processes: int = 1,
        box_files: Dict[str, Path] | None = None,
        compressed_box_files: Dict[str, Path] | None = None,
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
                # Deliberately `additional_files` and not `box_files`: the
                # caller owns that one - `evaluate` puts the checker's three
                # arguments in it - and merging the two would be one more thing
                # to keep in step. They cannot collide either way, since those
                # three are named with a leading dot.
                additional_files=self._run_artifacts(executable),
                box_files=box_files,
                compressed_box_files=compressed_box_files,
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
            compressed_box_files=compressed_box_files,
        )


# What a submission is called when its uploaded name reduces to nothing.
DEFAULT_SOURCE_STEM = "source"


def source_filename(filename: str, language: Language) -> str:
    """The name a submission's source is written under before it is compiled.

    The uploaded name and not a generated one, for the reason
    `prepare_evaluator` states about an uploaded checker: `javac` requires
    `Foo.java` for `public class Foo`, and `Language.compile` then looks for
    `Foo.class` because it derives the artifact from the stem. Writing every
    submission as `source.<ext>` made java the one language nobody could submit.

    The sanitising every other user-supplied path gets happens *here* rather
    than at upload: `post_submission` only takes a basename, and rows already in
    the database carry whatever that let through.

    The extension is the language's canonical one rather than the uploaded one.
    The extension is what tells a compiler what it has been handed: `Main.JAVA`
    is a java submission that javac would refuse to read, and a `.cc` and a
    `.cpp` are the same thing to g++.
    """
    try:
        stem = Path(safe_filename(filename)).stem
    except ValueError:
        stem = ""
    return f"{stem or DEFAULT_SOURCE_STEM}.{language.extensions[0]}"


# `languages.toml` is part of the package rather than a runtime setting - see
# the comment in `run_from_queue` on why it is deliberately not configurable -
# so it is resolved relative to this module and never to the working directory,
# the same rule `cocco/evaluators/` states for the bundled scripts. It lives
# *inside* `cocco/` rather than beside it, which is what lets an installed
# `cocco-judge` find it: a wheel has a package directory and no checkout.
LANGUAGES_PATH = Path(__file__).resolve().parent / "languages.toml"


def load_languages(path: Path = LANGUAGES_PATH) -> Dict[str, Language]:
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
        from_storage = False
    else:
        source = storage.resolve(evaluator_path)
        from_storage = True
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
    # A bundled evaluator lives in the source tree and is plain; an uploaded
    # one lives in storage and so is compressed, like everything there.
    if from_storage:
        expand(source, local)
    else:
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
        # The evaluator reads its three files by path; stdin is deliberately
        # empty, so that "what does my checker read?" has one answer.
        None,
        args=[_ACTUAL_IN_BOX, _EXPECTED_IN_BOX, _INPUT_IN_BOX],
        time_limit=EVALUATOR_TIME_LIMIT_S,
        mem_limit_kb=EVALUATOR_MEM_LIMIT_KB,
        # Which of the three is which is the boundary this judge draws:
        # storage is compressed and a box is plain, so the two files that come
        # out of storage are expanded into the box while the one written here
        # is copied.
        box_files={_ACTUAL_IN_BOX: actual_path},
        compressed_box_files={
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
    languages = load_languages()
    runtime: Language = languages[run_data.language]

    outcomes = []
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        # Under the student's own name, sanitised - see `source_filename`.
        source_fname = temp / source_filename(run_data.filename, runtime)
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
                    score = 0.0
                    eval_msg = exec_res.message or exec_res.status
                    outcome = _classify_isolate_failure(exec_res)
                    logger.info(
                        "submission %s test %s failed: status=%s message=%s",
                        run_data.submission_id,
                        test_case.id,
                        exec_res.status,
                        exec_res.message,
                        extra={
                            "testcase_id": test_case.id,
                            "outcome": outcome.value,
                            "elapsed_s": exec_res.elapsed_time_s,
                            "peak_memory_bytes": exec_res.max_rss_kb * 1024,
                        },
                    )
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
                        extra={
                            "testcase_id": test_case.id,
                            "outcome": outcome.value,
                            "score": score,
                            "elapsed_s": exec_res.elapsed_time_s,
                            "peak_memory_bytes": exec_res.max_rss_kb * 1024,
                        },
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

    # Everything below - the compile, every test case, the evaluator, the
    # outcome rows - is about this one submission, and is logged with these
    # fields without a single one of those lines naming them. That is what
    # makes "everything that happened to submission 812" one query.
    with bind(
        submission_id=run_data.submission_id,
        challenge_id=run_data.challenge_id,
        language=run_data.language,
    ):
        logger.info("picked up submission %s from queue", run_data.submission_id)

        outcomes: List[TestOutcome] = []
        try:
            # `languages.toml` is deliberately not configurable, unlike the
            # database and the storage root: it is shipped inside the package
            # and read from there by both processes, so a file pointed
            # elsewhere would be one more way for the two of them to disagree
            # in silence. What *is* configurable is the flake those runtimes
            # are built out of (`[judge] flake`) - a language entry names an
            # output some flake has to expose, and an installed judge has no
            # checkout to assume.
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
        # The headline score the pages show, computed the way they compute it
        # (the mean, out of 1) so the log and the standings agree.
        score = (
            sum(o.score for o in outcomes) / len(outcomes) if outcomes else None
        )
        logger.info(
            "submission %s done, recorded %d test outcomes",
            run_data.submission_id,
            len(outcomes),
            extra={"score": score, "n_tests": len(outcomes)},
        )

    return True


async def run_loop(engine: Engine, storage: Storage, wait_time=5):
    logger.info("judge run loop started (poll interval %ss)", wait_time)
    while True:
        has_run = await run_from_queue(engine, storage)
        if not has_run:
            logger.debug("judge waiting for %f seconds", wait_time)
            await asyncio.sleep(wait_time)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cocco-judge",
        description="Run the cocco judge loop, picking up queued submissions.",
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
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity for this run (default: [log] level, else INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """The `cocco-judge` console script.

    Module level, and sync, so that it is *importable*: this used to be an
    async function defined inside the `__main__` guard, which works for
    `python -m cocco.judge` and cannot be named by an entry point at all.
    Only `run_loop` is awaited - the engine, the storage root and the flake
    ref are settled before the event loop starts.
    """
    args = build_parser().parse_args(argv)

    # Logging from the `[log]` table alone, and then the rest of the file, so
    # that what `load_config` has to say - a missing file, a setting in its old
    # place, no session secret - lands in the judge's log in the judge's
    # format. A `[log]` table that cannot be read has no log to go to.
    try:
        configure_logging(load_log_config(args.config), "judge", args.log_level)
        config = load_config(args.config)
    except ConfigError as e:
        print(f"cocco-judge: {e}", file=sys.stderr)
        raise SystemExit(1)

    global FLAKE_REF, GCROOTS_DIR
    FLAKE_REF = config.judge.flake
    GCROOTS_DIR = config.judge.gcroots

    storage = Storage(config.storage.root)
    engine = create_engine(config.database.url)

    # The same refusal as the schema check below, for the storage root: every
    # file under it is compressed, and a root that predates that is a judge
    # whose every submission would fail on the first test case with the fix
    # buried in this log. Asked once, here, where it names the command.
    try:
        check_storage_format(config.storage.root)
    except StorageFormatError as e:
        logger.error("%s", e)
        raise SystemExit(1)

    # The judge never builds a schema and never migrates one: it is the process
    # that must not surprise anybody with DDL, and it used to call
    # `metadata.create_all` here, which silently papered over a web app and a
    # judge pointed at different databases. It asks once, at startup, and then
    # runs forever - so a `[dev] reset_db` boot of `cocco-web` underneath a live
    # judge leaves that judge on a schema that has been dropped and rebuilt.
    # `just dev` migrates first and starts the judge last, which is the ordinary
    # path; anything else is loud rather than wrong, because `run_from_queue`
    # has no `except` around its transaction.
    try:
        with engine.connect() as conn:
            check_schema_is_current(conn, process="cocco-judge")
    except SchemaOutOfDate as e:
        logger.error("%s", e)
        raise SystemExit(1)

    # After the schema check, and before the loop. After, because a database
    # that is behind is the commoner failure and costs nothing to notice -
    # there is no point pulling a JDK only to then refuse to start. Before,
    # because this is what puts a GC root on every runtime, and what makes a
    # flake that cannot build one a judge that does not come up rather than a
    # submission that fails whenever somebody first uses that language.
    logger.info(
        "building language runtimes from flake %s, rooted in %s",
        FLAKE_REF,
        GCROOTS_DIR,
    )
    try:
        asyncio.run(prewarm_runtimes(load_languages()))
    except RuntimeUnavailable as e:
        logger.error("%s", e)
        raise SystemExit(1)

    asyncio.run(run_loop(engine, storage))


if __name__ == "__main__":
    main()
