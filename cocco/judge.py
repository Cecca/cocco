import asyncio
import os
import shutil
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


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


async def resolve_runtime(runtime_name: str, bin_name: str) -> Path:
    p = await asyncio.create_subprocess_shell(
        " ".join(
            ["nix", "build", f".#{runtime_name}", "--no-link", "--print-out-paths"]
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await p.communicate()
    assert p.returncode == 0
    nix_store_paths: List[str] = stdout.strip().decode().splitlines()
    for path in nix_store_paths:
        binary = Path(f"{path}/bin/{bin_name}")
        if binary.is_file():
            return binary
    raise ValueError(f"could not find binary {bin_name} in {nix_store_paths}")


@dataclass
class RunOutcome(object):
    retcode: int
    stdout: str
    stderr: str
    elapsed_time_s: float
    max_rss_kb: int
    status: str
    message: str | None


async def isolate_run(
    exe: Path,
    stdin: Path,
    args: List[str],
    time_limit: float = 2.0,
    mem_limit_kb: int = 512 * 1024,
    processes: int = 1,
    additional_files: List[Path] = [],
) -> RunOutcome:
    async with _IDS_POOL.acquire() as box_id:
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
        assert isolate_init.returncode == 0, (
            f"call to isolate failed with output: {stderr.decode()}"
        )
        box_path = Path(stdout.decode().strip()) / "box"

        filtered_args = []
        for arg in args:
            if os.path.isfile(arg):
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
        with open(stdout_file) as fp:
            stdout = fp.read()
        with open(stderr_file) as fp:
            stderr = fp.read()

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
        assert isolate_cleanup.returncode == 0, f"cleanup failed:\n{stdout}\n{stderr}"

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

    async def compile(self, source: Path, workdir: Path) -> Path:
        if self.compiler is None:
            return source
        compiler = await resolve_runtime(self.runtime, self.compiler)
        output = workdir / (source.stem + self.output_ext)
        fmt_args = [
            a.format(source=str(source), output=str(output), workdir=str(workdir))
            for a in self.compiler_args
        ]
        proc = await asyncio.create_subprocess_exec(
            str(compiler),
            *fmt_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        assert proc.returncode == 0, f"compilation failed:\n{stderr.decode()}"
        return output

    async def run(
        self,
        executable: Path,
        stdin: Path,
        args: List[str] | None = None,
        time_limit: float = 2.0,
        mem_limit_kb: int = 262144,
        processes: int = 1,
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
        )


def load_languages(path: Path) -> Dict[str, Language]:
    with open(path, "rb") as fp:
        data = tomllib.load(fp)
    return {name: Language(name=name, **cfg) for name, cfg in data.items()}


class Evaluator(object):
    def evaluate(self, actual_output: str) -> tuple[float, str]:
        raise NotImplementedError("Not implemented")


class EqualityEvaluator(Evaluator):
    expected_output: str
    strip_whitespace: bool

    def __init__(self, expected_output: str, strip_whitespace: bool = True):
        if strip_whitespace:
            self.expected_output = expected_output.strip()
        else:
            self.expected_output = expected_output
        self.strip_whitespace = strip_whitespace

    def evaluate(self, actual_output: str) -> tuple[float, str]:
        actual_output = (
            actual_output.strip() if self.strip_whitespace else actual_output
        )
        if actual_output == self.expected_output:
            return (1, "")
        else:
            return (0, "Output is not the expected one:\n{expected_output}")


class DiffEvaluator(Evaluator):
    expected_lines: List[str]

    def __init__(self, expected_output: str):
        self.expected_lines = expected_output.splitlines(keepends=True)

    def evaluate(self, actual_output: str) -> tuple[float, str]:
        """Return the ratio of the diff lines as the score, and
        the actual diff as the message"""
        from difflib import Differ

        actual_lines = actual_output.splitlines(keepends=True)
        diff = list(Differ().compare(self.expected_lines, actual_lines))
        matching = sum(1 for line in diff if line.startswith("  "))
        total = len(self.expected_lines)
        score = matching / total if total > 0 else 1.0
        return score, "".join(diff)


class AccuracyEvaluator(Evaluator):
    """Interprets the text as a sequence of labels and compares the accuracy of
    a classificator."""

    ground_truth: List[str]

    def __init__(self, expected_output: str):
        self.ground_truth = expected_output.splitlines()

    def evaluate(self, actual_output: str) -> tuple[float, str]:
        actual_labels = actual_output.splitlines()
        total = len(self.ground_truth)
        if total == 0:
            return 1.0, ""
        matching = sum(
            1
            for expected, actual in zip(self.ground_truth, actual_labels)
            if expected == actual
        )
        score = matching / total
        return score, f"Accuracy: {matching}/{total}"


class F1Evaluator(Evaluator):
    """Interprets the text as a sequence of labels, one per line, and computes
    the macro-averaged F1 score of a classifier against the ground truth.

    The label set is the union of labels seen in the ground truth and in the
    actual output. For each label, precision, recall, and F1 are computed in a
    one-vs-rest fashion; the returned score is the unweighted mean of the
    per-class F1 values (macro-F1), so every class contributes equally
    regardless of its frequency. This makes the metric appropriate for
    imbalanced label distributions, where accuracy would be misleading.

    Score is in [0, 1]; the message contains per-class precision/recall/F1
    and the final macro-F1.
    """

    ground_truth: List[str]

    def __init__(self, expected_output: str):
        self.ground_truth = expected_output.splitlines()

    def evaluate(self, actual_output: str) -> tuple[float, str]:
        actual_labels = actual_output.splitlines()
        if len(self.ground_truth) == 0:
            return 1.0, ""

        labels = set(self.ground_truth) | set(actual_labels)
        per_class = []
        lines = []
        for label in sorted(labels):
            tp = fp = fn = 0
            for expected, actual in zip(self.ground_truth, actual_labels):
                if actual == label and expected == label:
                    tp += 1
                elif actual == label and expected != label:
                    fp += 1
                elif actual != label and expected == label:
                    fn += 1
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = (
                2 * precision * recall / (precision + recall)
                if (precision + recall) > 0
                else 0.0
            )
            per_class.append(f1)
            lines.append(f"{label}: P={precision:.3f} R={recall:.3f} F1={f1:.3f}")

        macro_f1 = sum(per_class) / len(per_class) if per_class else 0.0
        message = "\n".join(lines + [f"Macro-F1: {macro_f1:.3f}"])
        return macro_f1, message


class RMSEEvaluator(Evaluator):
    """Interprets both inputs as sequences of floats, one per line, and returns
    the Root Mean Squared Error between the actual output and the ground truth
    as the score.

    Unlike the other evaluators, a lower score indicates a better result (RMSE
    is in [0, +inf), with 0 meaning a perfect match). The message reports the
    RMSE, the number of paired samples, and any lines that could not be parsed
    as floats.
    """

    ground_truth: List[float]

    def __init__(self, expected_output: str):
        self.ground_truth = [float(line) for line in expected_output.splitlines()]

    def evaluate(self, actual_output: str) -> tuple[float, str]:
        actual_values: List[float] = []
        parse_errors: List[str] = []
        for i, line in enumerate(actual_output.splitlines()):
            try:
                actual_values.append(float(line))
            except ValueError:
                parse_errors.append(f"line {i + 1}: {line!r}")

        n = min(len(self.ground_truth), len(actual_values))
        if n == 0:
            return float("inf"), "No comparable float pairs"

        squared_errors = [
            (expected - actual) ** 2
            for expected, actual in zip(self.ground_truth[:n], actual_values[:n])
        ]
        rmse = (sum(squared_errors) / n) ** 0.5

        message_lines = [f"RMSE: {rmse:.6f}", f"Samples: {n}"]
        if len(self.ground_truth) != len(actual_values):
            message_lines.append(
                f"Length mismatch: expected {len(self.ground_truth)}, "
                f"got {len(actual_values)}"
            )
        if parse_errors:
            message_lines.append("Unparsable lines:")
            message_lines.extend(parse_errors)
        return rmse, "\n".join(message_lines)


# class Evaluator(object):
#     expected_output: List[str]
#     strip_whitespace: bool

#     def __init__(self, expected_output: str, strip_whitespace: bool = True):
#         self.expected_output = expected_output.splitlines()
#         self.strip_whitespace = strip_whitespace
#         if self.strip_whitespace:
#             self.expected_output = [line.strip() for line in expected_output]

#     def evaluate(self, actual_output: str) -> tuple[float, str]:
#         """Return the fraction of matched lines (optinally stripping whitespaces),
#         along with a diff.
#         """
#         from difflib import context_diff

#         total_lines = len(self.expected_output)
#         matching = 0
#         actual_lines: List[str] = actual_output.splitlines()
#         ic(self.expected_output)
#         ic(actual_lines)
#         if self.strip_whitespace:
#             actual_lines = [line.strip() for line in actual_lines]
#         for expected, actual in zip(self.expected_output, actual_lines):
#             if expected == actual:
#                 matching += 1

#         diff = "\n".join(context_diff(self.expected_output, actual_lines))

#         return (matching / total_lines), diff


if __name__ == "__main__":

    async def main():
        exe = await resolve_runtime("gcc", "gcc")
        print(exe)

    asyncio.run(main())
