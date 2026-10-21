import argparse
import asyncio
import functools
import gzip
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import tomllib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

import psycopg
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from cocco.repository import judging as repo
from cocco.config import (
    BOX_POOL_SIZE,
    DEFAULT_FLAKE_REF,
    ConfigError,
    config_path,
    default_gcroots,
    load_config,
    load_log_config,
)
from cocco.evaluators import DEFAULT_EVALUATOR, bundled_path
from cocco.exercises import HARNESS_PATH
from cocco.exercises import format as exercise_format
from cocco.exercises.compare import Comparison, Rules, compare
from cocco.logs import bind, configure_logging
from cocco.schema import (
    Challenge,
    EvaluatorResult,
    Exercise,
    ExerciseBuildResult,
    ExerciseCheckResult,
    ExerciseExecution,
    ExerciseJobError,
    ExerciseJobRunData,
    ExerciseProblem,
    ExerciseRunResult,
    ExerciseTest,
    ExerciseTestResult,
    ExerciseVerdict,
    HarnessException,
    HarnessReport,
    Submission,
    SubmissionRunData,
    TestCase,
    TestOutcome,
)
from cocco.migrate import SchemaOutOfDate, check_schema_is_current
from cocco.model import (
    ExerciseJobKind,
    ExerciseVersionStatus,
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

    def __init__(self, num_boxes: int = BOX_POOL_SIZE):
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
# `run_submission` and `run_loop`, for the reason `core.DUMMY_AUTH` is one:
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
    fsize_kb: int | None = None,
    env: Dict[str, str] | None = None,
) -> RunOutcome:
    """Run `exe` in a fresh isolate box.

    `stdin` is `None` for an empty standard input, or a **compressed file in
    storage**, which is decompressed into the box's stdin as it runs. One rule
    with no flag to get wrong: everything under the storage root is gzip, and
    nothing else is ever passed here.

    `args` are passed to the program **exactly as written**, always, and a
    file reaches the box in exactly three ways, every one of them explicit:
    `box_files` (a box-relative name to put a file under, whatever it is
    called outside - the only way to be sure two unrelated paths do not land
    on the same name), `compressed_box_files` (the same, for a file out of
    storage, which is expanded rather than copied; a separate argument because
    a caller mixing the two up would be silent), and `additional_files` (each
    under its basename). The box is one flat directory, and an argument that
    names a file names it by its box-relative name.

    There used to be a fourth way: any argument that happened to name an
    existing file or directory was copied in and rewritten to its basename.
    That guesswork resolved a bare word against the judge's *working
    directory*, which under systemd is `/` - so a student's `public class run`,
    whose class name java is given as an argument, copied `/run`, credentials
    and all, into a box the student could read. Never reintroduce it.

    Ordering is load-bearing and easy to reverse by accident: `box_files` and
    `compressed_box_files` are placed first, then `additional_files`, so the
    later one silently wins a name collision.

    Two limits beyond the caller's CPU `time_limit`. A wall-clock one,
    `2 * time_limit + 1` seconds, because `--time` counts CPU and a program
    that sleeps - `time.sleep`, `signal.pause`, a blocking read - uses none,
    and would hold a box and a worker for ever; it ends as status `TO`, like
    the CPU one. And `fsize_kb`, isolate's cap on every file the program
    writes - its stdout and stderr included, which this function then reads
    whole into memory - for a caller that knows how much output is plenty.
    `env` is set in the box, whose environment isolate otherwise empties.
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
        # From here on the box is initialised, and it is cleaned up whatever
        # happens - a cancelled worker included, which is now routine (a judge
        # stopping, a restart) and used to leave the box initialised for the
        # next `--init` on that id to trip over. A run still in progress is
        # stopped first, because a box cannot be cleaned up under it - with
        # SIGTERM and not SIGKILL: isolate kills the box's processes on
        # SIGTERM, while SIGKILL takes out only isolate itself and leaves the
        # program running in its cgroup, unlimited, with the box still busy.
        isolate_run = None
        try:
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
                f"--wall-time={2 * time_limit + 1}",
                f"--cg-mem={mem_limit_kb}",
                *([f"--fsize={fsize_kb}"] if fsize_kb is not None else []),
                *(f"--env={k}={v}" for k, v in (env or {}).items()),
                f"--processes={processes}",
                "--dir=/nix/store",  # this allows running the compilers/interpreters
                f"--meta={meta_file}",
                f"--stdout={stdout_file.name}",
                f"--stderr={stderr_file.name}",
                "--run",
                "--",
                str(exe),
                *args,
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
        except BaseException:
            if isolate_run is not None and isolate_run.returncode is None:
                with suppress(ProcessLookupError):
                    isolate_run.terminate()
                try:
                    await asyncio.wait_for(isolate_run.wait(), 10)
                except TimeoutError:
                    # The last resort, which leaves the box busy: logged
                    # below when the cleanup fails, rather than a judge that
                    # cannot stop.
                    with suppress(ProcessLookupError):
                        isolate_run.kill()
                    await isolate_run.wait()
            # Logged rather than raised: the exception on its way out says what
            # went wrong, and a cleanup failure must not replace it.
            try:
                await _cleanup_box(box_id)
            except Exception:
                logger.exception("could not clean up box %s", box_id)
            raise

        await _cleanup_box(box_id)
        return ret


async def _cleanup_box(box_id: int) -> None:
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
            # Box-relative, all three: `isolate_run` passes arguments as they
            # are and copies nothing on their say-so, and `_run_artifacts`
            # below puts the executable - and, for java, every class beside
            # it - in the box under its basename. The box is the program's
            # working directory, so that is `{workdir}`.
            fmt = dict(
                executable=executable.name,
                workdir=".",
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
    #
    # Nothing in the claim may await for real: it holds a pooled sync
    # connection, and with several workers on one loop, a claim that yielded
    # would let the others pile up connections behind it.
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
        except asyncio.CancelledError:
            # The judge is stopping - SIGTERM, a restart - with this one half
            # graded. It goes back on the queue for the next judge rather than
            # staying `running` forever, which is not just a stale badge: a
            # running submission refuses every regrade and every delete of its
            # challenge until somebody fixes the row by hand.
            # As for an exercise job: nothing here may stop the cancellation.
            try:
                with engine.begin() as conn:
                    repo.requeue_submission(conn, run_data.submission_id)
                logger.info(
                    "submission %s interrupted; put back on the queue",
                    run_data.submission_id,
                )
            except Exception:
                logger.exception(
                    "submission %s interrupted, and could not be put back",
                    run_data.submission_id,
                )
            raise
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


########################################################################
# Exercises
#
# An exercise job never runs the student's file directly: every program, the
# author's solution included, goes through `cocco/exercises/harness.py` in a box
# of its own, and the judge reads what it did off the last line of the box's
# stdout (`schema.HarnessReport`). The comparison against the expected output
# then happens here, in this process, because the rules are declarative and
# the code applying them is ours - an evaluator box per test would double the
# cost of a check to buy nothing.

# What one exercise test may use. The time is the exercise's own (`time:` in
# the file); the memory is fixed, an exercise being a few lines of python on a
# few lines of input, and the process budget is the python language's, as for
# a challenge.
EXERCISE_MEM_LIMIT_KB = 256 * 1024

# isolate's `--fsize` for a harness run: the most any file the program writes
# may hold, its stdout included - which is where the report goes, and which
# the judge reads whole. The report of a program that printed right up to the
# harness's 256 K-character caps is at most about 9 MB, every character
# escaped to `\uXXXX\uXXXX`; anything past this is a program writing to fd 1
# directly, which then ends without reporting.
EXERCISE_FSIZE_KB = 16 * 1024

# Set in every harness box: string hashing, and so the order a `set` prints
# in, is then the same in the author's solution and in the student's program.
EXERCISE_ENV = {"PYTHONHASHSEED": "0"}

# What is kept of a program's stdout, stderr and traceback in a job's result.
# The harness already caps what it captures at 256 K characters, and the
# comparison sees all of that; what is *stored* is far less, because a check
# stores every test and a flood on each would be megabytes of JSONB per click.
#
# A field never keeps more than `MAX_EXERCISE_STORED_OUTPUT`; a check's tests
# that did not pass share `MAX_EXERCISE_RESULT_OUTPUT` between their three
# fields each; and a test that passed keeps `PASSED_TEST_STORED_OUTPUT`, its
# output being, as far as the rules can tell, the expected one the page
# already has. Enough of a flood to see how it began.
MAX_EXERCISE_STORED_OUTPUT = 32 * 1024
MAX_EXERCISE_RESULT_OUTPUT = 256 * 1024
PASSED_TEST_STORED_OUTPUT = 1024

# A build problem's `detail` - a traceback, a stderr - as the author page shows
# it under the sentence.
MAX_PROBLEM_DETAIL = 2000

# The names the harness and the program take inside the box. `main.py` is the
# name tracebacks and line numbers are reported against, which is the student's
# own file as far as they are concerned.
_HARNESS_IN_BOX = "harness.py"
_MAIN_IN_BOX = "main.py"

# Code points PostgreSQL will not hold in JSONB: a NUL, which it refuses in
# `text` too, and a lone surrogate, which is not a character at all. A python
# `str` keeps a valid astral character as one code point, so anything in this
# range is lone. Both can come out of the harness's JSON - `\u0000` and
# `\ud800` are valid escapes there - because a program can print them.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _jsonb_safe(value):
    """`value` with every string fit for JSONB: NULs dropped, lone surrogates
    replaced with U+FFFD. Recurses through dicts and lists.

    Applied to the harness's report as soon as it is parsed, rather than to
    the result on its way into the database: a solution's output becomes an
    expected output, and if the stored copy lost a NUL that the student's
    output kept, a correct program would fail. One boundary, so everything the
    comparison sees is what gets stored. A judge error's message is the only
    other text that reaches a result, and goes through it too.
    """
    if isinstance(value, str):
        return _SURROGATE.sub("�", value.replace("\x00", ""))
    if isinstance(value, dict):
        return {k: _jsonb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonb_safe(v) for v in value]
    return value


def parse_harness_report(stdout: str) -> HarnessReport | None:
    """The harness's report, from the last line of a box's stdout, or None.

    The *last* line, because a program can write to fd 1 directly, ahead of
    the report. None when that line is not a report: the program ended the
    process first, or wrote over it. A forgery that does parse is accepted -
    the output is still compared here, so it can only fake the exception
    check, and exercises grade nothing.
    """
    last = stdout.rstrip("\n").rpartition("\n")[2]
    try:
        return HarnessReport.model_validate(_jsonb_safe(json.loads(last)))
    # `RecursionError` because the last line is the program's to write: a
    # forged `[[[[…` nests deeper than the decoder will follow.
    except (ValueError, ValidationError, RecursionError):
        return None


def read_execution(res: RunOutcome) -> ExerciseExecution:
    """What a harness run did, from isolate's outcome.

    The status says how the run ended and the report what the program did. A
    timeout and an OOM produce no report, by construction; any other ending
    without one is the program's doing (`no_report`), never the judge's. Only
    isolate's own failure (`XX`) is the judge's, and raises.
    """
    if res.status == "TO":
        return ExerciseExecution(
            status="timeout", message=res.message, elapsed_s=res.elapsed_time_s
        )
    if res.oom:
        return ExerciseExecution(
            status="memory", message=res.message, elapsed_s=res.elapsed_time_s
        )
    if res.status == "XX":
        raise RuntimeError(f"isolate failed: {res.message or res.status}")
    report = parse_harness_report(res.stdout) if res.status == "OK" else None
    if report is None:
        return ExerciseExecution(
            status="no_report",
            # Not the report, which is missing: whatever reached the box's
            # descriptors - the program's direct writes, or should the harness
            # itself ever break, its traceback.
            stdout=_jsonb_safe(res.stdout),
            stderr=_jsonb_safe(res.stderr),
            message=res.message,
            elapsed_s=res.elapsed_time_s,
        )
    return ExerciseExecution(
        status="ok",
        stdout=report.stdout,
        stderr=report.stderr,
        truncated=report.truncated,
        exit=report.exit,
        exception=report.exception,
        elapsed_s=res.elapsed_time_s,
    )


def _write_stdin(path: Path, text: str) -> Path:
    """`text` as the compressed file `isolate_run` takes for a stdin.

    Compressed although it never was in storage: `isolate_run` has one rule
    for its stdin, and a second, plaintext kind would be the flag to get wrong
    that the rule exists to avoid. A few bytes, at the fastest level.
    """
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as fp:
        fp.write(text)
    return path


async def run_in_harness(
    language: Language,
    mode: str,
    program: Path,
    stdin: str,
    time_limit: float,
    workdir: Path,
) -> ExerciseExecution:
    """Run `program` through the harness in a box, in `check` or `run` mode.

    `workdir` must be private to the caller and its stdin file is named
    uniquely in it, so the tests of a job can run at once - each in its own
    box, from the shared pool.
    """
    assert language.interpreter is not None
    interpreter = await resolve_runtime(language.runtime, language.interpreter)
    stdin_path = None
    if stdin:
        fd, name = tempfile.mkstemp(dir=workdir, prefix="stdin-")
        os.close(fd)
        stdin_path = _write_stdin(Path(name), stdin)
    res = await isolate_run(
        interpreter,
        stdin_path,
        [_HARNESS_IN_BOX, mode, _MAIN_IN_BOX],
        time_limit=time_limit,
        mem_limit_kb=EXERCISE_MEM_LIMIT_KB,
        processes=1 + language.additional_processes,
        box_files={_HARNESS_IN_BOX: HARNESS_PATH, _MAIN_IN_BOX: program},
        fsize_kb=EXERCISE_FSIZE_KB,
        env=EXERCISE_ENV,
    )
    return read_execution(res)


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _stored(
    execution: ExerciseExecution, limit: int = MAX_EXERCISE_STORED_OUTPUT
) -> dict:
    """An execution's fields as a result stores them: stdout, stderr and the
    traceback each cut at `limit`, and `clipped` set when that cut anything.
    `truncated` stays the harness's, which is what the verdict was about."""
    limit = min(limit, MAX_EXERCISE_STORED_OUTPUT)
    stdout, cut_out = _cap(execution.stdout, limit)
    stderr, cut_err = _cap(execution.stderr, limit)
    exception = execution.exception
    cut_tb = False
    if exception is not None:
        traceback, cut_tb = _cap(exception.traceback, limit)
        exception = exception.model_copy(update={"traceback": traceback})
    return {
        **dict(execution),
        "stdout": stdout,
        "stderr": stderr,
        "exception": exception,
        "clipped": execution.clipped or cut_out or cut_err or cut_tb,
    }


def error_matches(exception: HarnessException | None, expected: str) -> bool:
    """Whether a program that raised `exception` ended with `expected`: that
    class or a subclass of it, which is why the harness reports the MRO."""
    return exception is not None and expected in exception.classes


def grade(
    execution: ExerciseExecution,
    expected_output: str,
    expected_error: str | None,
    rules: Rules,
) -> tuple[ExerciseVerdict, Comparison | None]:
    """The verdict on one test, and the comparison when there was one.

    A port of the prototype's `checkTests`, in its order: how the run ended,
    then a flood, then the exception, then the output. The output printed
    before an expected exception is compared as usual. Quadratic in the line
    counts with `order` and `numbers` together, so it is called off the event
    loop.
    """
    if execution.status != "ok":
        return execution.status, None
    if execution.truncated:
        return "truncated", None
    clean = execution.exception is None and execution.exit == 0
    if expected_error and clean:
        return "noerror", None
    if (
        not error_matches(execution.exception, expected_error)
        if expected_error
        else not clean
    ):
        return "error", None
    comparison = compare(expected_output, execution.stdout, rules)
    return ("pass" if comparison.ok else "fail"), comparison


async def _run_tests(
    language: Language,
    exercise: Exercise,
    program: Path,
    tests: list[ExerciseTest],
    workdir: Path,
) -> list[ExerciseExecution]:
    """`program` on every one of `tests`, at once, each in its own box.

    A TaskGroup rather than `gather`: when one run raises - isolate itself
    failing - the others are cancelled, and so cleaned up, instead of carrying
    on unwatched after the job has already been reported as broken.
    """
    async with asyncio.TaskGroup() as tasks:
        runs = [
            tasks.create_task(
                run_in_harness(
                    language, "check", program, t.input, exercise.time_s, workdir
                )
            )
            for t in tests
        ]
    return [run.result() for run in runs]


async def check_exercise(
    language: Language, exercise: Exercise, source: str, workdir: Path
) -> ExerciseCheckResult:
    """Run `source` on every test of `exercise` and grade it.

    The exercise must come from a built version: every test's
    `expected_output` is what the build computed.
    """
    program = workdir / _MAIN_IN_BOX
    program.write_text(source, encoding="utf-8")
    executions = await _run_tests(language, exercise, program, exercise.tests, workdir)
    graded = []
    for test, execution in zip(exercise.tests, executions):
        assert test.expected_output is not None
        graded.append(
            await asyncio.to_thread(
                grade, execution, test.expected_output, test.expected_error, exercise.rules
            )
        )
    # The result's budget, shared by the tests that did not pass: three
    # fields each.
    failed = sum(verdict != "pass" for verdict, _ in graded)
    share = MAX_EXERCISE_RESULT_OUTPUT // (3 * failed) if failed else 0
    results = [
        ExerciseTestResult(
            **_stored(
                execution, PASSED_TEST_STORED_OUTPUT if verdict == "pass" else share
            ),
            verdict=verdict,
            hidden=test.hidden,
            diff=sorted(comparison.diff) if comparison else [],
            unordered=comparison.unordered if comparison else False,
        )
        for test, execution, (verdict, comparison) in zip(
            exercise.tests, executions, graded
        )
    ]
    return ExerciseCheckResult(
        passed=all(r.verdict == "pass" for r in results), tests=results
    )


@functools.cache
def _languages() -> Dict[str, Language]:
    """`languages.toml`, read once: it is part of the package, so it cannot
    change under a running judge, and a Run is too small a job to be reading
    a file for."""
    return load_languages()


def _python(language_name: str) -> Language:
    """The language an exercise document names. Only an interpreted one can
    run the harness, and today that is python."""
    language = _languages().get(language_name)
    if language is None or not language.is_interpreted:
        raise ValueError(f"exercises cannot run in language {language_name!r}")
    return language


async def run_exercise_job(engine: Engine, job: ExerciseJobRunData):
    """A check or a run: its result, in one of the shapes `schema` pins."""
    assert job.exercise_index is not None and job.source is not None
    with engine.connect() as conn:
        found = repo.query_job_exercise(conn, job.version_id, job.exercise_index)
    if found is None:
        return ExerciseJobError(
            message=(
                f"Version {job.version_id} has no exercise {job.exercise_index}, "
                "or is not built."
            )
        )
    language_name, exercise = found
    language = _python(language_name)
    with tempfile.TemporaryDirectory() as temp:
        workdir = Path(temp)
        if job.kind is ExerciseJobKind.check:
            return await check_exercise(language, exercise, job.source, workdir)
        program = workdir / _MAIN_IN_BOX
        program.write_text(job.source, encoding="utf-8")
        execution = await run_in_harness(
            language, "run", program, job.stdin or "", exercise.time_s, workdir
        )
        return ExerciseRunResult(**_stored(execution))


@dataclass(frozen=True)
class VersionBuild:
    """What a build writes on its version: all three, once."""

    status: ExerciseVersionStatus
    problems: list[ExerciseProblem]
    document: dict | None


def _detail(execution: ExerciseExecution) -> str | None:
    text = (
        execution.exception.traceback
        if execution.exception is not None
        else execution.stderr
    ).strip()
    return text[-MAX_PROBLEM_DETAIL:] or None


async def build_exercise(
    language: Language, n: int, exercise: Exercise, workdir: Path
) -> tuple[Exercise, list[ExerciseProblem]]:
    """One exercise of a build: its expected outputs, and its problems.

    A port of the prototype's `checkExercise`. The solution runs on every
    test; then a second time on the tests where its output became the expected
    one, so that a solution printing something random is caught - and only
    there, since a run that failed has nothing to be compared with. Then the
    starter runs on the result, which should not already pass.
    """
    label = f'exercise {n}, "{exercise.title}"'
    problems: list[ExerciseProblem] = []

    def problem(level, text, test=None, detail=None):
        problems.append(
            ExerciseProblem(
                level=level, where=label, text=text, exercise=n, test=test,
                detail=detail,
            )
        )

    tests = list(exercise.tests)
    if exercise.solution is not None:
        solution = workdir / "solution.py"
        solution.write_text(exercise.solution, encoding="utf-8")
        first = await _run_tests(language, exercise, solution, tests, workdir)
        # The tests whose expected output is the first run's.
        computed: list[int] = []
        for k, (t, r) in enumerate(zip(tests, first)):
            test_no = k + 1
            name = f"test {test_no}{' (hidden)' if t.hidden else ''}"
            if r.status == "timeout":
                problem(
                    "error",
                    f"The solution does not finish within {exercise.time_s:g} s on {name}.",
                    test_no,
                )
                continue
            if r.status == "memory":
                problem("error", f"The solution runs out of memory on {name}.", test_no)
                continue
            if r.status == "no_report":
                problem(
                    "error",
                    f"The solution ended without reporting on {name}"
                    + (f" ({r.message})." if r.message else "."),
                    test_no,
                    _detail(r),
                )
                continue
            if r.truncated:
                problem("error", f"The solution prints too much output on {name}.", test_no)
                continue
            expected_error = None
            if t.error is None:
                if r.exception is not None or r.exit != 0:
                    tip = (
                        f' If that is intended, add an "error" block with '
                        f"{r.exception.type} after the input."
                        if r.exception is not None
                        else ""
                    )
                    problem(
                        "error",
                        f"The solution ends with an error on {name}.{tip}",
                        test_no,
                        _detail(r),
                    )
                    continue
            elif r.exception is None:
                ended = (
                    f"exited with code {r.exit}" if r.exit != 0 else "ended normally"
                )
                wanted = t.error or 'an error, as the "error" block says'
                problem(
                    "error",
                    f"On {name} the solution should end with {wanted}, but it {ended}.",
                    test_no,
                )
                continue
            elif t.error and not error_matches(r.exception, t.error):
                problem(
                    "error",
                    f"On {name} the solution ends with {r.exception.type}, "
                    f'but the "error" block says {t.error}.',
                    test_no,
                    _detail(r),
                )
                continue
            else:
                expected_error = t.error or r.exception.type
            if t.output is not None:
                if not (await asyncio.to_thread(compare, t.output, r.stdout, exercise.rules)).ok:
                    problem(
                        "warning",
                        f'On {name} the output written in the "output" block differs '
                        'from the solution\'s. Using the "output" block.',
                        test_no,
                    )
                expected_output = t.output
            else:
                expected_output = r.stdout
                computed.append(k)
            if not expected_output.strip() and not expected_error:
                problem("warning", f"The solution prints nothing on {name}.", test_no)
            tests[k] = t.model_copy(
                update={
                    "expected_output": expected_output,
                    "expected_error": expected_error,
                }
            )
        if computed:
            second = await _run_tests(
                language, exercise, solution, [tests[k] for k in computed], workdir
            )
            for k, again in zip(computed, second):
                if again.status == "ok" and again.stdout != first[k].stdout:
                    name = f"test {k + 1}{' (hidden)' if tests[k].hidden else ''}"
                    problem(
                        "warning",
                        f"The solution gives different outputs in two runs of {name} "
                        "(random numbers?): students may not be able to pass it.",
                        k + 1,
                    )
    else:
        # `format.parse` refuses a file without a solution where one is needed,
        # so every test here has its output written out.
        tests = [
            t.model_copy(
                update={"expected_output": t.output, "expected_error": t.error or None}
            )
            for t in tests
        ]

    built = exercise.model_copy(update={"tests": tests})
    if any(t.expected_output is None for t in tests):
        return built, problems

    if exercise.starter.strip():
        starter_dir = workdir / "starter"
        starter_dir.mkdir()
        result = await check_exercise(language, built, exercise.starter, starter_dir)
        if result.passed:
            problem("warning", "The starter code already passes every test.")
    return built, problems


async def build_version(source: str, title: str) -> VersionBuild:
    """Compile a version: parse its source again, then run every exercise.

    Parsed again rather than trusting whatever the import made of it, because
    the source is the one thing a version is sure of. The exercises one after
    another, and within one exercise three rounds one after another - the
    solution, the solution again, the starter - each round's tests at once. So
    a build never asks the box pool for more than one exercise's tests at a
    time, as a check does, and a run that arrives meanwhile waits behind a
    handful of boxes rather than behind every test of the set: the pool hands
    boxes out in the order they were asked for, and knows nothing of priority.
    """
    parsed = exercise_format.parse(source, default_title=title)
    problems = list(parsed.problems)
    if parsed.document is None:
        return VersionBuild(ExerciseVersionStatus.failed, problems, None)
    document = parsed.document
    language = _python(document.language)

    exercises = []
    with tempfile.TemporaryDirectory() as temp:
        for n, exercise in enumerate(document.exercises, start=1):
            workdir = Path(temp) / str(n)
            workdir.mkdir()
            built, found = await build_exercise(language, n, exercise, workdir)
            exercises.append(built)
            problems.extend(found)

    if any(p.level == "error" for p in problems):
        return VersionBuild(ExerciseVersionStatus.failed, problems, None)
    document = document.model_copy(update={"exercises": exercises})
    return VersionBuild(
        ExerciseVersionStatus.ready, problems, document.model_dump(mode="json")
    )


async def build_job(engine: Engine, job: ExerciseJobRunData) -> VersionBuild:
    with engine.connect() as conn:
        version = repo.query_version_to_build(conn, job.version_id)
    if version is None:
        raise ValueError(f"version {job.version_id} does not exist")
    return await build_version(version.source, version.title)


async def run_exercise_from_queue(engine: Engine, kind: ExerciseJobKind) -> bool:
    """`run_from_queue` for one kind of exercise job.

    The same two promises: a judge stopped mid-job puts it back on the queue,
    and a failure stays with the job it happened in - recorded as its result
    (`ExerciseJobError`), so a polling tab sees an answer rather than a job
    that is `running` forever. A build that fails that way fails its version
    too, which otherwise would stay `building` with nothing left to finish it.
    """
    with engine.connect() as conn:
        job = repo.pull_queued_exercise_job(conn, kind)
    if job is None:
        return False

    with bind(
        exercise_job_id=job.id,
        job_kind=job.kind.value,
        version_id=job.version_id,
        session_id=job.session_id,
        exercise_index=job.exercise_index,
    ):
        logger.info("picked up %s job %s from queue", job.kind.value, job.id)
        build: VersionBuild | None = None
        try:
            if job.kind is ExerciseJobKind.build:
                build = await build_job(engine, job)
                result = ExerciseBuildResult(
                    status=build.status.value,
                    errors=sum(p.level == "error" for p in build.problems),
                    warnings=sum(p.level == "warning" for p in build.problems),
                )
            else:
                result = await run_exercise_job(engine, job)
        except asyncio.CancelledError:
            # Nothing in here may stop the cancellation from going on its way:
            # a worker that swallowed it would keep `run_loop`'s TaskGroup
            # waiting, and the judge would hang on SIGTERM. A failure to put
            # the job back is logged, and the job is left `running`.
            try:
                with engine.begin() as conn:
                    requeued = repo.requeue_exercise_job(
                        conn,
                        job.id,
                        ExerciseJobError(
                            message="Interrupted: the judge stopped while running "
                            "this, and a newer one had already been queued."
                        ).model_dump(mode="json"),
                    )
                logger.info(
                    "%s job %s interrupted; %s",
                    job.kind.value,
                    job.id,
                    "put back on the queue" if requeued else "a newer one is queued",
                )
            except Exception:
                logger.exception(
                    "%s job %s interrupted, and could not be put back",
                    job.kind.value,
                    job.id,
                )
            raise
        except Exception as e:
            logger.exception("%s job %s failed in the judge", job.kind.value, job.id)
            message = _jsonb_safe(f"Judge internal error: {e!r}")[:MAX_OUTPUT_LENGTH]
            result = ExerciseJobError(message=message)
            if job.kind is ExerciseJobKind.build:
                build = VersionBuild(
                    ExerciseVersionStatus.failed,
                    [
                        ExerciseProblem(
                            level="error",
                            where="file",
                            text="The judge failed while building this version. "
                            "Import the file again; if it fails the same way, "
                            "this is a bug in cocco.",
                            detail=message,
                        )
                    ],
                    None,
                )

        with engine.begin() as conn:
            if build is not None:
                written = repo.finish_version_build(
                    conn,
                    job.version_id,
                    build.status,
                    [p.model_dump(mode="json") for p in build.problems],
                    build.document,
                )
                if isinstance(result, ExerciseBuildResult):
                    result = result.model_copy(update={"written": written})
            repo.finish_exercise_job(conn, job.id, result.model_dump(mode="json"))
        logger.info(
            "%s job %s done",
            job.kind.value,
            job.id,
            # Named apart from the access log's `status`, which is an HTTP code.
            extra={
                "result_kind": result.kind,
                "passed": getattr(result, "passed", None),
                "build_status": getattr(result, "status", None),
            },
        )
    return True


def exercise_queue(kind: ExerciseJobKind) -> Callable[[Engine, Storage], Awaitable[bool]]:
    """One entry of `QUEUES`: the oldest queued exercise job of `kind`. The
    storage is not used - exercises keep their tests in the database."""

    async def take(engine: Engine, storage: Storage) -> bool:
        return await run_exercise_from_queue(engine, kind)

    take.__name__ = take.__qualname__ = f"run_{kind.value}_from_queue"
    return take


# How often an idle judge looks for run jobs past `repo.RUN_JOB_RETENTION`. An
# hour, per process: the retention is six months, and a DELETE on every idle
# tick of every worker would be thousands of them a day to find nothing.
PRUNE_INTERVAL_S = 3600.0
_next_prune = 0.0


# Rows per DELETE, each in a transaction of its own: the first prune after a
# term can find tens of thousands, and one statement taking them all would hold
# its locks for as long.
PRUNE_BATCH = 1000


def _prune(engine: Engine) -> int:
    pruned = 0
    while True:
        with engine.begin() as conn:
            batch = repo.prune_run_jobs(conn, PRUNE_BATCH)
        pruned += batch
        if batch < PRUNE_BATCH:
            return pruned


async def prune_if_due(engine: Engine) -> None:
    """Delete old run jobs, at most once per `PRUNE_INTERVAL_S`, on a thread -
    unlike a claim, a prune can take long enough to stall every other worker.
    A failure is logged and left for the next interval: pruning is
    housekeeping, and the worker that tried has a queue to get back to."""
    global _next_prune
    now = time.monotonic()
    if now < _next_prune:
        return
    _next_prune = now + PRUNE_INTERVAL_S
    try:
        pruned = await asyncio.to_thread(_prune, engine)
    except Exception:
        logger.exception("could not prune old run jobs")
        return
    if pruned:
        logger.info("pruned %d run jobs older than %s", pruned, repo.RUN_JOB_RETENTION)


# Where a free worker looks for its next job, highest priority first: each entry
# claims one job off its queue and runs it to the end, answering whether there
# was one. A worker asks them in this order every time it is free, so the
# priority is strict - nothing on a later queue starts while an earlier one has
# work, however long it has waited: run > check > build > challenge submission.
# A student staring at Run is the most latency-sensitive person in the
# building, a Check is an attempt somebody is waiting on, a build is staff
# waiting on an import, and a regrade of 300 submissions must not hold a lesson
# up.
QUEUES: tuple[Callable[[Engine, Storage], Awaitable[bool]], ...] = (
    exercise_queue(ExerciseJobKind.run),
    exercise_queue(ExerciseJobKind.check),
    exercise_queue(ExerciseJobKind.build),
    run_from_queue,
)


async def run_next(engine: Engine, storage: Storage) -> bool:
    """Run the most urgent job there is, if there is one."""
    for take in QUEUES:
        if await take(engine, storage):
            return True
    return False


class Wakeup:
    """What an idle worker sleeps on, so that a notification ends the sleep.

    A worker takes a `ticket` *before* it looks at the queue and sleeps on that
    ticket after finding nothing. `ring` sets the current ticket and hands out a
    fresh one, so a notification that lands between the look and the sleep has
    already set the ticket the worker is about to sleep on, and the sleep
    returns at once - rather than the job waiting out the poll interval for a
    wake-up that came a moment too early to be heard.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def ticket(self) -> asyncio.Event:
        return self._event

    def ring(self) -> None:
        self._event.set()
        self._event = asyncio.Event()


async def worker(engine: Engine, storage: Storage, wakeup: Wakeup, wait_time: float):
    """Claim and run one job at a time, forever.

    A failure stays with the job it happened in: logged, and the worker carries
    on after the poll interval - a plain sleep rather than the ticket, so a
    failure that repeats (the database gone) is not retried in a tight loop
    every time somebody submits. Every worker is woken by every notification
    and all but one find nothing; that is a handful of empty claims, and
    cheaper than deciding which worker should go. A worker that finds nothing
    is also the one that prunes old run jobs, when that is due.
    """
    while True:
        ticket = wakeup.ticket()
        try:
            ran = await run_next(engine, storage)
        except Exception:
            logger.exception("judge worker failed; carrying on in %ss", wait_time)
            await asyncio.sleep(wait_time)
            continue
        if not ran:
            await prune_if_due(engine)
            logger.debug("judge worker waiting for up to %f seconds", wait_time)
            with suppress(TimeoutError):
                await asyncio.wait_for(ticket.wait(), wait_time)


def _libpq_url(engine: Engine) -> str:
    """The engine's database as a URL psycopg will take on its own.

    The same URL with the `+psycopg` dropped, so the listener is pointed at
    exactly the database the claims go to - an empty `postgresql+psycopg://`
    included, which becomes `postgresql://` and is resolved by libpq from the
    same PG* variables.
    """
    return engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )


async def listen_for_work(engine: Engine, wakeup: Wakeup, retry_s: float):
    """Ring `wakeup` whenever the web app enqueues something (`notify_queue`).

    Its own connection, and an async psycopg one rather than one out of the
    judge's sync engine: it spends its whole life parked on a socket waiting
    for the server, which is what the event loop is for. Losing it costs
    latency and nothing else, because the workers still poll - so a failure is
    a warning and a reconnect, never a judge that stops. It rings once on every
    (re)connect too, for whatever was enqueued while nobody was listening.
    """
    listen = sql.SQL("LISTEN {}").format(sql.Identifier(repo.QUEUE_CHANNEL))
    while True:
        try:
            # Keepalives, because a connection that only ever waits sends
            # nothing: dropped by a firewall or a failover, it would wait on a
            # dead socket forever and the judge would quietly fall back to the
            # poll. With them, the kernel notices within a couple of minutes.
            async with await psycopg.AsyncConnection.connect(
                _libpq_url(engine),
                autocommit=True,
                keepalives=1,
                keepalives_idle=60,
                keepalives_interval=10,
                keepalives_count=6,
            ) as conn:
                await conn.execute(listen)
                logger.debug("listening on %s", repo.QUEUE_CHANNEL)
                wakeup.ring()
                async for _ in conn.notifies():
                    wakeup.ring()
        except psycopg.Error as e:
            logger.warning(
                "lost the notification channel (%s); polling until it is back", e
            )
        await asyncio.sleep(retry_s)


async def run_loop(
    engine: Engine, storage: Storage, concurrency: int = 1, wait_time: float = 5
):
    """`concurrency` workers, each claiming and running one job at a time, and
    the listener that wakes them.

    Each worker claims on the event loop with the sync engine, which blocks the
    others for the millisecond a claim takes and no longer: `SKIP LOCKED` means
    a claim never waits on another claimer's lock. The box pool, not this, is
    what bounds the sandboxes running at once.

    It runs until it is cancelled, which is how the judge stops: every worker
    then puts its half-graded submission back on the queue (`run_from_queue`)
    and cleans up its boxes (`isolate_run`) on the way out.
    """
    logger.info(
        "judge run loop started (%d workers, poll interval %ss)",
        concurrency,
        wait_time,
    )
    wakeup = Wakeup()
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(listen_for_work(engine, wakeup, wait_time))
        for _ in range(concurrency):
            tasks.create_task(worker(engine, storage, wakeup, wait_time))


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
    # Migrating first and starting the judge last is the ordinary path;
    # anything else is loud rather than wrong, because `run_from_queue` has no
    # `except` around its transaction.
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

    asyncio.run(_serve(engine, storage, config.judge.concurrency))
    logger.info("judge stopped")


async def _serve(engine: Engine, storage: Storage, concurrency: int) -> None:
    """`run_loop` until SIGTERM, which is how systemd stops a unit.

    SIGTERM's default is to die on the spot, leaving every submission in flight
    `running` and every box initialised; cancelling instead is what lets each
    worker put its submission back and clean up. Ctrl-C needs no handler -
    `asyncio.run` already turns SIGINT into the same cancellation.
    """
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    with suppress(asyncio.CancelledError):
        await run_loop(engine, storage, concurrency)


if __name__ == "__main__":
    main()
