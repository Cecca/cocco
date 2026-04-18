import asyncio
import os
import shutil
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from icecream import ic


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
        " ".join(["nix", "build", f".#{runtime_name}", "--no-link", "--print-out-paths"]),
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
    additional_files: List[Path] = []
) -> RunOutcome:
    async with _IDS_POOL.acquire() as box_id:
        isolate_init = await asyncio.create_subprocess_exec(
            "isolate", "--cg", "--box-id", str(box_id), "--init",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
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
            "isolate", "--cg", "--box-id", str(box_id), "--cleanup",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
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
            str(compiler), *fmt_args,
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
        processes:int = 1
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
                interpreter, stdin, [*prefix_args, *args],
                time_limit=time_limit, mem_limit_kb=mem_limit_kb,
                processes=processes+self.additional_processes,
                additional_files=[executable]
            )
        return await isolate_run(
            # For compiled executables, we strip the absolute path, which is instead needed
            # for the interpreters above
            Path(executable.name), stdin, args,
            time_limit=time_limit, mem_limit_kb=mem_limit_kb,
            processes=processes+self.additional_processes,
            additional_files=[executable]
        )


def load_languages(path: Path) -> Dict[str, Language]:
    with open(path, "rb") as fp:
        data = tomllib.load(fp)
    return {name: Language(name=name, **cfg) for name, cfg in data.items()}


if __name__ == "__main__":
    async def main():
        exe = await resolve_runtime("gcc", "gcc")
        print(exe)

    asyncio.run(main())

