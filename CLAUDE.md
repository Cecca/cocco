# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`cocco` is a self-hosted judge for programming challenges. Submissions are executed inside the [isolate](https://github.com/ioi/isolate) sandbox (the IOI/ICPC judge), using language runtimes supplied by a Nix flake. The HTTP layer is FastAPI; persistence is SQLModel on SQLite.

`DESIGN.md` has the rationale for the isolate + Nix choice and the alternatives that were rejected; read it before proposing architectural changes.

## Commands

Dependencies are managed with `uv` (Python 3.13). A `.envrc` / Nix `devShell` auto-creates `.venv` and activates it.

- Install deps: `uv sync`
- Run the API (dev): `uv run fastapi dev cocco/main.py`
- Run all tests: `uv run pytest`
- Run only unit tests (skip those requiring isolate + nix): `uv run pytest -m "not integration"`
- Run a single test: `uv run pytest cocco/test_judge.py::test_language_python`
- Build a language runtime on its own: `nix build .#python3` (or `.#gcc`, `.#jdk`)

Integration tests (`@pytest.mark.integration`) require `isolate` installed with its setuid bit, a working `nix` with flakes, and the current user being allowed to run isolate boxes. On NixOS, isolate is declared via `security.wrappers`; on other distros install it outside Nix.

## Architecture

### Execution pipeline (`cocco/judge.py`)

Three layers, loosely coupled:

1. **`resolve_runtime(runtime_name, bin_name)`** — shells out to `nix build .#<runtime>` and returns the `/nix/store` path to the requested binary. Runtimes are whatever the flake exposes (`python3`, `gcc`, `jdk`). The full store is later bind-mounted into the sandbox via `--dir=/nix/store` so the interpreter/compiler can find its libraries.

2. **`isolate_run(exe, stdin, args, ...)`** — the sandbox primitive. Acquires a box id from `_IDS_POOL` (a bounded `asyncio.Queue` of ids, so concurrent runs don't collide on isolate box numbers), runs `isolate --init`, copies any file/dir arguments into the box, runs `isolate --run --cg` with cgroup-based time and memory limits, parses the `--meta` file, then `isolate --cleanup`. Returns a `RunOutcome` with retcode, stdout, stderr, elapsed time, peak RSS, and the isolate status/message.

3. **`Language`** (loaded from `languages.toml` via `load_languages`) — glues the two together for each language. Three shapes, chosen by which of `compiler`/`interpreter` are set:
   - interpreted (e.g. python): `interpreter` only — the interpreter runs inside isolate with the source copied in.
   - compiled native (e.g. c, cpp): `compiler` only — compile outside isolate, run the binary inside.
   - compiled + VM (e.g. java): both — `javac` outside isolate, `java` inside, with `additional_processes` added to the sandbox process budget for the JVM.

   Compiler args support `{source}`, `{output}`, `{workdir}`; interpreter args support `{executable}`, `{workdir}`, `{name}`. When adding a language, edit `languages.toml` and, if a new runtime is needed, add a package output in `flake.nix`.

### Evaluators (`cocco/judge.py`)

An `Evaluator` turns an actual output string into a `(score, message)` pair. Subclasses: `EqualityEvaluator`, `DiffEvaluator` (ratio of matching lines via `difflib.Differ`), `AccuracyEvaluator`, `F1Evaluator` (macro-F1 over the union of labels), `RMSEEvaluator`. Note that `RMSEEvaluator` returns a score where lower is better — all others are higher-is-better in `[0, 1]`.

### Data model (`cocco/model.py`)

SQLModel tables form the domain: `User` (with `UserRole`), `Course` → `Challenge` → `TestCase` (inputs/expected outputs referenced by filesystem path, not stored in the DB — see `DESIGN.md` "Test case inputs"), `Submission` → `TestOutcome`, and `QueuedSubmission` for async judging. `enqueue_submission` adds to the queue but does not commit; the caller controls the transaction.

### HTTP layer (`cocco/main.py`)

FastAPI app exposed as `cocco.main:app` (see `[tool.fastapi]` in `pyproject.toml`). Currently a stub — the judging pipeline is not yet wired to HTTP endpoints.

## Conventions specific to this repo

- **Async everywhere in `judge.py`.** Subprocess calls use `asyncio.create_subprocess_exec`; tests are `async def` and rely on `asyncio_mode = "auto"` in `pyproject.toml`, so no `@pytest.mark.asyncio` is needed.
- **Box ids are a shared resource.** Always go through `_IDS_POOL.acquire()` rather than hardcoding a box id; the pool defaults to 32 concurrent boxes.
- **Large inputs live on disk.** Test case inputs/outputs are passed by path, not read into memory, and isolate takes stdin directly from a file descriptor. Preserve this when adding features.
- **Language runtimes are pinned by `flake.lock`.** Changing a compiler version means updating the flake, not the system.
