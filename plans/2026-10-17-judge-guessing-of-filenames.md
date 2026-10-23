---
status: pending
---

# The judge's working directory, and whether `isolate_run` should guess files again

## Background

Until `c85a93d`, `isolate_run` copied into the box any argument that happened to name an existing file or directory,
and rewrote the argument to its basename:

```python
elif os.path.isfile(arg):   shutil.copy(arg, box_path / arg.name)
elif os.path.isdir(arg):    shutil.copytree(arg, box_path / arg.name)
```

A bare word resolves against the judge's **working directory**, which under systemd is `/`. java receives the class
name (`{name}` in `languages.toml`) as an argument, so a student's `public class run` copied `/run` (sockets, service
credentials, other units' runtime state) into a box the student's own program could read.

`c85a93d` deleted the guess. Today an argument is passed exactly as written, and a file reaches a box only when the
caller names it in `box_files`, `compressed_box_files` or `additional_files`. Every caller was already doing so or
was changed to:

| Caller | Arguments | How the files arrive |
| --- | --- | --- |
| `Language.run`, interpreted (python, java) | `{executable}` → basename, `{workdir}` → `.`, `{name}` → stem | `additional_files=_run_artifacts(executable)` |
| `Language.run`, compiled (c, cpp) | caller's `args` | `additional_files=[executable]`, exe as `Path(executable.name)` |
| `evaluate` | `.actual_output .expected_output .test_input` | `box_files` / `compressed_box_files` |
| exercise jobs (`isolate_run` direct) | `harness.py <mode> main.py` | `box_files` |

The draft suggests a simpler fix might be enough: refuse to start the judge unless its working directory is not `/`
(and possibly empty), and keep the old guessing. This plan weighs that against the current behaviour.

## Options

### A. Restore the guess, guard the working directory (the draft's proposal)

`main()` refuses to start when `Path.cwd()` is `/`, or is not empty.

What it costs:

- **"Not `/`" does not close the hole.** The leak is "a bare word the student controls names something on the judge
  host". Any non-empty working directory has the same problem with different contents. The usual dev setup,
  `just judge` from the checkout, is a good example: `public class node_modules` would `copytree` the whole of
  `node_modules` into every box (disk and time, once per test case); `public class cocco` copies the package source,
  the bundled evaluators and the tests. A relative `[storage] root` (allowed by `config.py`) would be a valid
  Java identifier sitting in the working directory: every student's submissions and every hidden expected output.
- **"Empty" breaks what the working directory is used for today.** `[judge] flake` defaults to `"."`, the flake *in
  the working directory*, and a relative `[storage] root` / `[judge] gcroots` resolve against it too. A checkout can
  never be an empty working directory, so the guard would have to be off in dev, which leaves dev exposed and the
  guard untested where people actually run it.
- **It is checked once, at startup.** Nothing keeps the directory empty afterwards.
- **It also guessed on absolute paths.** `os.path.isfile("/etc/…")` needs no working directory at all. No argument
  that reaches `isolate_run` today is both student-controlled and able to hold a `/` (`{name}` is the stem of a
  `safe_filename` result), but the first feature that passes per-test-case or teacher-written arguments (an
  obvious next step for challenges) would reopen the hole with no working directory involved. Course staff are not
  trusted with the judge host.
- **What an argument means would depend on the host.** `run` would reach the program as `run` on one judge and the
  name of a copied directory on another. The guess is also what the `expected_output.c` collision in `judge.md` came
  from.
- **It is not simpler in code.** The guess was 17 lines; the explicit form removed them, and every caller already
  names its files. Restoring it means adding the guess back *and* adding the guard.

What it would buy: callers could pass an absolute path in `args` and have it copied. No current caller wants that.

### B. Keep explicit files (status quo)

Nothing is guessed, so the working directory never matters to the box. Already shipped and covered by
`test_a_java_class_named_like_a_directory_copies_nothing`, `test_a_python_argument_is_never_resolved_against_the_working_directory`
(`test_judge.py`) and `test_a_bare_word_argument_is_not_copied_from_the_working_directory` (`test_exercise_judge.py`).

### C. B, plus a working directory that holds nothing (defence in depth)

The useful part of the draft's idea, without the guess: make the judge's working directory irrelevant by
construction rather than by checking it. After loading the config, `main()` turns every working-directory-relative
setting into an absolute one, then `chdir`s into a fresh, empty, private directory it owns. Any future code that
resolves a bare word on the host (a reintroduced guess, a careless `Path(arg).exists()`, a library doing the same)
then finds nothing, on systemd and in a checkout alike.

## Recommendation

**C.** Do not restore the guess. The `/` check addresses the symptom we happened to hit (`/run`) and not the
cause (host paths named by student input), and it costs more code than it saves. C keeps the property B has and adds
the one thing A was reaching for, that the judge's cwd is never a source of files, without depending on how the
judge was started.

If C is judged not worth it, B alone is sound: close this plan as "keep `c85a93d`" with no code change.

## Plan for C

1. **Absolutise relative settings in `judge.main()`, before anything else uses them.**
   - `config.storage.root` and `config.judge.gcroots`: `Path.absolute()` (not `resolve()`: `build_runtime` is
     careful not to resolve through the gcroot symlinks, see the comment at `cocco/judge.py:164`).
   - `config.judge.flake`: only when it is a *path* flake reference. nix treats `.`/`..`, and a ref starting with
     `./`, `../` or `/` (or `path:`), as a path; `github:…`, `git+…` and registry names are left alone. Write this as
     a small function in `config.py` beside `DEFAULT_FLAKE_REF`, so the rule has one home and a unit test.
   - These must happen *before* `FLAKE_REF` / `GCROOTS_DIR` are assigned and before `Storage(...)` and
     `check_storage_format` run.
2. **Enter a private empty directory.** `tempfile.mkdtemp(prefix="cocco-judge-")`, `chmod` it to `0o500` so not
   even the judge can write into it by accident, `os.chdir` into it, and remove it on exit (`try/finally` around
   `asyncio.run(_serve(...))`; a hard kill leaves an empty directory in `/tmp`, which is harmless). Log the directory
   at startup, at the same level as the existing "prewarming" lines.
   - Only `main()` does this. `isolate_run` and the tests keep using whatever the process's cwd is, so the existing
     `monkeypatch.chdir` tests keep proving that B holds on its own.
   - Confirm nothing after startup depends on the old cwd: `nix build` gets an absolute flake ref and out-link,
     the meta file is `/tmp/meta-<id>.txt`, `HARNESS_PATH` and the bundled evaluators come from `__file__`, and the
     config file was read before the `chdir`. Check `cocco-judge`'s other subcommands, if any, the same way.
3. **Web app: no change.** `cocco-web` never resolves student input against the filesystem outside `Storage`, which
   already refuses paths outside its root.
4. **Tests** (unit, not `integration`, unless noted):
   - the flake-ref rule: `"."` and `"../x"` become absolute; `"github:o/r"`, `"git+https://…"`, `"nixpkgs"`,
     `"path:/abs"` are unchanged or correctly handled;
   - `main()` with a relative storage root and gcroots: the `Storage` and `GCROOTS_DIR` it ends up with are
     absolute and point where the relative ones did (patch `_serve` and the schema check, the way
     `test_entrypoints.py` drives `main()` today; check what it already patches);
   - after `main()` sets up, `Path.cwd()` is empty and not writable, and it is gone after a clean exit;
   - keep the three existing guessing tests as they are.
5. **Docs.**
   - `.claude/rules/judge.md`: one sentence after the "nothing reaches it by guesswork" bullet saying the judge
     also runs from a private empty directory, and that this is defence in depth, not the fix (so nobody reads it as
     licence to reintroduce the guess).
   - `cocco.example.toml`, at `[judge] flake`: say that a relative path is resolved against the directory the judge
     was *started* in, once, at startup.
   - `isolate_run`'s docstring already says "Never reintroduce it"; leave it.

## Out of scope

- Validating `args` inside `isolate_run` (e.g. refusing anything that names an existing host path). It would be the
  guess's check without its action, and would refuse legitimate words like `run`.
- Any systemd unit (`WorkingDirectory=`, `ProtectSystem=` etc.). We do not ship one, and C makes the judge correct
  without one. Worth a line in the deployment notes if a unit is ever added.
