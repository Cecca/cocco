import asyncio
import contextlib
import dataclasses
import gzip
import tempfile
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text

import cocco.judge as cj
from cocco import model as cm
from cocco.avatar import generate_identicon
from cocco.repository.challenges import insert_submission
from cocco.storage import Storage


def _stdin(directory: Path, contents: str) -> Path:
    """A compressed standard input, which is the only kind `isolate_run` takes.

    Everything under the storage root is gzip and that is where a test case's
    input comes from, so a test writing a plaintext file here would be pinning
    a contract the judge does not have.
    """
    path = directory / "stdin.txt"
    with gzip.open(path, "wt") as fp:
        fp.write(contents)
    return path


@pytest.mark.parametrize(
    "language,uploaded,expected",
    [
        # The whole point: javac wants `public class Main` in `Main.java`.
        ("java", "Main.java", "Main.java"),
        # The extension is the language's own, not the upload's - javac would
        # refuse to read a file it does not consider a source file at all.
        ("java", "Main.JAVA", "Main.java"),
        # ... and g++ is happy either way, so the canonical one is no loss.
        ("cpp", "solution.cc", "solution.cpp"),
        # Sanitised here, because `post_submission` only takes a basename and
        # rows already in the database carry whatever that let through.
        ("java", "../../etc/Passwd.java", "Passwd.java"),
        ("java", "we ird/na:me.java", "na_me.java"),
        # A dotfile is not rejected, it is de-dotted: `safe_filename` strips
        # leading dots, so what is left is the stem.
        ("java", ".java", "java.java"),
        # Nothing usable left at all: a name, not a crash and not a path.
        ("java", "", "source.java"),
        ("python", "...", "source.py"),
    ],
)
def test_source_filename(language: str, uploaded: str, expected: str):
    languages = cj.load_languages()
    assert cj.source_filename(uploaded, languages[language]) == expected


async def test_resolve_runtime_is_a_lookup(monkeypatch):
    """A runtime `prewarm_runtimes` never covered is a bug, said so here rather
    than papered over by starting a `nix build` in the middle of somebody's
    submission. This is a unit test at all only because resolution stopped
    shelling out - it needs neither nix nor a flake."""
    monkeypatch.setattr(cj, "_RUNTIME_CACHE", {})
    with pytest.raises(cj.RuntimeUnavailable):
        await cj.resolve_runtime("nosuchruntime", "nosuchbinary")


@pytest.mark.integration
async def test_runtimes_are_rooted_against_the_garbage_collector():
    """The bug this was written for.

    `nix build --no-link` left the store path held by nothing nix could see, so
    a `nix-store --gc` on the judge host was free to collect an interpreter a
    running judge was still handing to isolate. The symlink asserted here is
    what makes nix register the indirect root that stops it.
    """
    link = cj.GCROOTS_DIR / "python3"
    assert link.is_symlink()
    assert str(link.resolve()).startswith("/nix/store/")
    # And it roots the very path the cache hands out, not merely something of
    # the same name: a link to some *other* build would root the wrong closure.
    python = await cj.resolve_runtime("python3", "python")
    assert python.is_relative_to(link.resolve())


@pytest.mark.integration
async def test_python_run():
    python = await cj.resolve_runtime("python3", "python")
    with tempfile.TemporaryDirectory(delete=False) as tmp:
        tmp = Path(tmp)
        script_path = tmp / "script.py"
        with open(script_path, "w") as fp:
            fp.write(textwrap.dedent("""\
            print("hello", input())
            """))
        stdin_path = _stdin(tmp, "moon")
        res = await cj.isolate_run(
            python,
            stdin_path,
            ["script.py"],
            box_files={"script.py": script_path},
        )
        
    assert res.stdout.strip() == "hello moon"


@pytest.mark.integration
async def test_language_python():
    python = cj.load_languages()["python"]
    with tempfile.TemporaryDirectory(delete=False) as tmp:
        tmp = Path(tmp)
        script_path = tmp / "script.py"
        with open(script_path, "w") as fp:
            fp.write(textwrap.dedent("""\
            print("hello", input())
            """))
        stdin_path = _stdin(tmp, "moon")

        exe = await python.compile(script_path, tmp)
        assert exe.success and exe.executable is not None
        res = await python.run(exe.executable, stdin_path)
    assert res.stdout.strip() == "hello moon"


@pytest.mark.integration
async def test_language_c():
    c = cj.load_languages()["c"]
    with tempfile.TemporaryDirectory(delete=False) as tmp:
        tmp = Path(tmp)
        script_path = tmp / "hello.c"
        with open(script_path, "w") as fp:
            fp.write(textwrap.dedent("""\
            #include <stdio.h>

            int main(void) {
                char word[256];
                if (scanf("%255s", word) != 1) {
                    return 1;
                }
                printf("hello %s\\n", word);
                return 0;
            }
            """))
        stdin_path = _stdin(tmp, "moon")

        exe = await c.compile(script_path, tmp)
        assert exe.success and exe.executable is not None
        res = await c.run(exe.executable, stdin_path)
    assert res.stdout.strip() == "hello moon"


@pytest.mark.integration
async def test_compile_without_artifact_is_a_failure(tmp_path):
    # A compiler that succeeds but writes no `{output}` is never silent.
    c = cj.load_languages()["c"]
    no_output = dataclasses.replace(c, compiler_args=["-fsyntax-only", "{source}"])
    src = tmp_path / "hello.c"
    src.write_text("int main(void) { return 0; }\n")
    res = await no_output.compile(src, tmp_path)
    assert not res.success and res.executable is None
    assert "the compiler produced no hello" in res.stderr


@pytest.mark.integration
async def test_compile_out_of_memory_is_a_compile_failure(tmp_path):
    # `#include "/dev/zero"` makes cc1 read until the cgroup limit: an OOM that
    # gcc reports as a plain `RE`, which must still be recognised.
    c = cj.load_languages()["c"]
    src = tmp_path / "hello.c"
    src.write_text('const char d[] = {\n#include "/dev/zero"\n};\nint main(void) { return 0; }\n')
    res = await c.compile(src, tmp_path)
    assert not res.success and res.executable is None
    assert "compilation ran out of memory (limit 1024 MB)" in res.stderr


@pytest.mark.integration
async def test_language_cpp():
    cpp = cj.load_languages()["cpp"]
    with tempfile.TemporaryDirectory(delete=False) as tmp:
        tmp = Path(tmp)
        script_path = tmp / "hello.cpp"
        with open(script_path, "w") as fp:
            fp.write(textwrap.dedent("""\
            #include <iostream>
            #include <string>

            int main() {
                std::string word;
                if (!(std::cin >> word)) {
                    return 1;
                }
                std::cout << "hello " << word << std::endl;
                return 0;
            }
            """))
        stdin_path = _stdin(tmp, "moon")

        exe = await cpp.compile(script_path, tmp)
        assert exe.success and exe.executable is not None
        res = await cpp.run(exe.executable, stdin_path)
    assert res.stdout.strip() == "hello moon"


@pytest.mark.integration
async def test_language_java():
    python = cj.load_languages()["java"]
    with tempfile.TemporaryDirectory(delete=False) as tmp:
        tmp = Path(tmp)
        script_path = tmp / "Test.java"
        with open(script_path, "w") as fp:
            fp.write(textwrap.dedent("""\
            import java.util.Scanner;
            public class Test {
                public static void main(String [] args) {
                    Scanner scanner = new Scanner(System.in);
                    System.out.println("hello " + scanner.next());
                }
            }
            """))
        stdin_path = _stdin(tmp, "moon")

        exe = await python.compile(script_path, tmp)
        assert exe.success and exe.executable is not None
        res = await python.run(exe.executable, stdin_path)
    assert res.stdout.strip() == "hello moon"


def _decoy(cwd: Path, name: str) -> None:
    """A directory called `name`, with a secret in it, in what will be the
    judge's working directory - `/run` under systemd."""
    (cwd / name).mkdir()
    (cwd / name / "secret").write_text("credentials")


@pytest.mark.integration
async def test_a_python_argument_is_never_resolved_against_the_working_directory(
    tmp_path, monkeypatch
):
    """`{executable}` reaches the box as `run.py`, a box-relative name, and a
    directory of that name where the judge runs is not copied in its place."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _decoy(cwd, "run.py")
    monkeypatch.chdir(cwd)
    python = cj.load_languages()["python"]
    script = tmp_path / "run.py"
    script.write_text(
        "import os, sys\n"
        "print(sys.argv, sorted(os.listdir('.')), os.path.isfile('run.py'))\n"
    )
    res = await python.run(script, None)
    assert res.stdout == "['run.py'] ['_stderr', '_stdout', 'run.py'] True\n"


@pytest.mark.integration
async def test_a_java_class_named_like_a_directory_copies_nothing(
    tmp_path, monkeypatch
):
    """The exploit: java is handed the class name, `{name}`, as an argument,
    and `public class run` used to copy `/run` into a box the student could
    read. It is passed as the word it is."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _decoy(cwd, "run")
    monkeypatch.chdir(cwd)
    java = cj.load_languages()["java"]
    build = tmp_path / "build"
    build.mkdir()
    source = build / "run.java"
    source.write_text(textwrap.dedent("""\
        import java.io.File;
        import java.util.Arrays;
        public class run {
            public static void main(String[] args) {
                String[] names = new File(".").list();
                Arrays.sort(names);
                System.out.println(String.join(",", names));
                System.out.println(new File("run").isDirectory());
            }
        }
        """))
    compiled = await java.compile(source, build)
    assert compiled.success and compiled.executable is not None
    res = await java.run(compiled.executable, None)
    assert res.stdout == "_stderr,_stdout,run.class\nfalse\n", res.stderr


# Needs isolate and nix like every other test in this file: it was missing the
# marker, so `pytest -m "not integration"` used to try to run a sandbox.
@pytest.mark.integration
async def test_run_submission():
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))

    stdin_path = Path("test-run-from-queue/stdin.txt")
    expected_path = Path("test-run-from-queue/expected.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")
    assert storage.resolve(stdin_path).is_file()
    assert storage.resolve(expected_path).is_file()

    test_cases = [
        cs.TestCase(
            execution_order=0,
            input_path=str(stdin_path),
            expected_output_path=str(expected_path),
        )
    ]

    def make_run_data(
        language: str,
        source: str,
        evaluator_path: str | None = None,
        filename: str = "solution.py",
    ) -> cs.SubmissionRunData:
        return cs.SubmissionRunData(
            submission_id=1,
            language=language,
            source=source,
            filename=filename,
            max_threads=1,
            max_memory_bytes=64 * 1024 * 1024,
            max_time_s=1.0,
            test_cases=test_cases,
            evaluator_path=evaluator_path,
        )

    # correct submission, graded by the bundled default
    run_data = make_run_data("python", 'print("hello", input())')
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.success
    assert res[0].score == 1

    # failing submission, by its score
    run_data = make_run_data("python", 'print("wrong!", input())')
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.success
    assert res[0].score == 0

    # submission that crashes
    run_data = make_run_data("python", 'import sys; sys.exit(127)')
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.runtime_error
    assert res[0].score == 0

    # submission that does not compile
    run_data = make_run_data(
        "c",
        """
            int main() {
                asdfasdfg; // <- this does not compile
                return 0;
            }
        """,
        filename="solution.c",
    )
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.compile_failure
    assert res[0].score == 0


@pytest.mark.integration
async def test_run_submission_java_keeps_its_class_name():
    """A java submission is written under its uploaded name, with every class.

    Two bugs in one test, because in practice they arrive together. Writing the
    source as `source.java` made javac refuse `public class Main` outright, and
    copying only the entry point's `.class` into the box left `Main$Helper` -
    and every other class javac emitted - behind, for a NoClassDefFoundError at
    the first line that used one.
    """
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))
    stdin_path = Path("test-run-java/stdin.txt")
    expected_path = Path("test-run-java/expected.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")

    run_data = cs.SubmissionRunData(
        submission_id=1,
        language="java",
        filename="Main.java",
        source=textwrap.dedent("""\
        import java.util.Scanner;

        public class Main {
            public static void main(String[] args) {
                Scanner scanner = new Scanner(System.in);
                System.out.println(Greeter.greet(scanner.next()));
            }
        }

        class Greeter {
            static String greet(String who) {
                return "hello " + who;
            }
        }
        """),
        max_threads=1,
        max_memory_bytes=256 * 1024 * 1024,
        max_time_s=10.0,
        test_cases=[
            cs.TestCase(
                execution_order=0,
                input_path=str(stdin_path),
                expected_output_path=str(expected_path),
            )
        ],
    )

    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.success, res[0].message
    assert res[0].score == 1


def _write_evaluator(storage: Storage, name: str, source: str) -> str:
    path = f"test-evaluators/{name}"
    storage.write(path, textwrap.dedent(source))
    return path


@pytest.mark.integration
async def test_run_submission_with_uploaded_evaluator():
    """A challenge's own evaluator grades it, in whatever language it is in."""
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))
    stdin_path = Path("test-run-from-queue/stdin.txt")
    expected_path = Path("test-run-from-queue/expected.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")

    test_cases = [
        cs.TestCase(
            execution_order=0,
            input_path=str(stdin_path),
            expected_output_path=str(expected_path),
        )
    ]

    def make_run_data(evaluator_path: str | None) -> cs.SubmissionRunData:
        return cs.SubmissionRunData(
            submission_id=1,
            language="python",
            source='print("hello", input())',
            filename="solution.py",
            max_threads=1,
            max_memory_bytes=64 * 1024 * 1024,
            max_time_s=1.0,
            test_cases=test_cases,
            evaluator_path=evaluator_path,
        )

    # A partial-credit checker: the score is whatever it says, not 0 or 1.
    half = _write_evaluator(
        storage,
        "half.py",
        """\
        import json, sys
        json.dump({"score": 0.5, "message": "half marks"}, sys.stdout)
        """,
    )
    res = await cj.run_submission(storage, make_run_data(half))
    assert res[0].outcome == cs.OutcomeEnum.success
    assert res[0].score == 0.5
    assert res[0].message == "half marks"

    # A checker in a different language from the submission. This is the whole
    # point of the feature, and the only thing that proves the two compiles do
    # not tread on each other.
    cpp = _write_evaluator(
        storage,
        "checker.cpp",
        """\
        #include <fstream>
        #include <iostream>
        #include <sstream>
        #include <string>

        std::string slurp(const char *path) {
            std::ifstream in(path);
            std::stringstream ss;
            ss << in.rdbuf();
            std::string s = ss.str();
            while (!s.empty() && isspace((unsigned char)s.back())) s.pop_back();
            return s;
        }

        int main(int argc, char **argv) {
            std::string actual = slurp(argv[1]);
            std::string expected = slurp(argv[2]);
            std::cout << "{\\"score\\": "
                      << (actual == expected ? "1.0" : "0.0")
                      << ", \\"message\\": \\"from c++\\"}";
            return 0;
        }
        """,
    )
    res = await cj.run_submission(storage, make_run_data(cpp))
    assert res[0].outcome == cs.OutcomeEnum.success
    assert res[0].score == 1.0
    assert res[0].message == "from c++"


@pytest.mark.integration
async def test_evaluator_receives_its_three_files():
    """argv is (actual, expected, input), and all three really arrive."""
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))
    stdin_path = Path("test-evaluator-argv/stdin.txt")
    expected_path = Path("test-evaluator-argv/expected.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")

    echo = _write_evaluator(
        storage,
        "echo.py",
        """\
        import json, sys
        parts = [open(p).read().strip() for p in sys.argv[1:4]]
        json.dump({"score": 1.0, "message": "|".join(parts)}, sys.stdout)
        """,
    )
    run_data = cs.SubmissionRunData(
        submission_id=1,
        language="python",
        source='print("hello", input())',
        filename="solution.py",
        max_threads=1,
        max_memory_bytes=64 * 1024 * 1024,
        max_time_s=1.0,
        test_cases=[
            cs.TestCase(
                execution_order=0,
                input_path=str(stdin_path),
                expected_output_path=str(expected_path),
            )
        ],
        evaluator_path=echo,
    )
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.success
    assert res[0].message == "hello moon|hello moon|moon"


@pytest.mark.integration
async def test_evaluator_files_do_not_collide_by_name():
    """The three files reach the box under names nothing else can claim.

    Isolate's box is one flat directory, so a naive implementation passing the
    real paths would have the evaluator's compiled binary - named after its
    source, `expected_output` - land on top of the expected output it is
    supposed to read.
    """
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))
    stdin_path = Path("test-evaluator-collision/test_input")
    expected_path = Path("test-evaluator-collision/expected_output.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")

    checker = _write_evaluator(
        storage,
        "expected_output.c",
        """\
        #include <ctype.h>
        #include <stdio.h>
        #include <string.h>

        void slurp(const char *path, char *out, size_t n) {
            FILE *fp = fopen(path, "r");
            if (!fp) { out[0] = 0; return; }
            size_t got = fread(out, 1, n - 1, fp);
            while (got > 0 && isspace((unsigned char)out[got - 1])) got--;
            out[got] = 0;
            fclose(fp);
        }

        int main(int argc, char **argv) {
            char actual[256], expected[256];
            slurp(argv[1], actual, sizeof(actual));
            slurp(argv[2], expected, sizeof(expected));
            printf("{\\"score\\": %s, \\"message\\": \\"%s\\"}",
                   strcmp(actual, expected) == 0 ? "1.0" : "0.0", expected);
            return 0;
        }
        """,
    )
    run_data = cs.SubmissionRunData(
        submission_id=1,
        language="python",
        source='print("hello", input())',
        filename="solution.py",
        max_threads=1,
        max_memory_bytes=64 * 1024 * 1024,
        max_time_s=1.0,
        test_cases=[
            cs.TestCase(
                execution_order=0,
                input_path=str(stdin_path),
                expected_output_path=str(expected_path),
            )
        ],
        evaluator_path=checker,
    )
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.success
    assert res[0].score == 1.0
    # Echoed back from the file the checker opened as argv[2]: had the compiled
    # binary landed on top of it, this would be the head of an ELF image.
    assert res[0].message == "hello moon"


@pytest.mark.integration
@pytest.mark.parametrize(
    "name,source",
    [
        # Exits non-zero: never a verdict on the submission.
        ("boom.py", "import sys\nsys.exit(1)\n"),
        # Prints something that is not a result.
        ("chatty.py", "print('looks good to me')\n"),
        # Runs forever: must not be reported as the submission timing out.
        ("hang.py", "while True:\n    pass\n"),
    ],
)
async def test_broken_evaluator_is_an_evaluator_error(name: str, source: str):
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))
    stdin_path = Path("test-broken-evaluator/stdin.txt")
    expected_path = Path("test-broken-evaluator/expected.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")

    run_data = cs.SubmissionRunData(
        submission_id=1,
        language="python",
        source='print("hello", input())',
        filename="solution.py",
        max_threads=1,
        max_memory_bytes=64 * 1024 * 1024,
        max_time_s=1.0,
        test_cases=[
            cs.TestCase(
                execution_order=0,
                input_path=str(stdin_path),
                expected_output_path=str(expected_path),
            )
        ],
        evaluator_path=_write_evaluator(storage, name, source),
    )
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.evaluator_error
    assert res[0].score == 0


@pytest.mark.integration
async def test_evaluator_that_does_not_compile_fails_every_test():
    import cocco.schema as cs

    storage = Storage(Path("/tmp/cocco-storage"))
    storage.write("test-uncompilable/stdin.txt", "moon")
    storage.write("test-uncompilable/expected.txt", "hello moon")

    run_data = cs.SubmissionRunData(
        submission_id=1,
        language="python",
        source='print("hello", input())',
        filename="solution.py",
        max_threads=1,
        max_memory_bytes=64 * 1024 * 1024,
        max_time_s=1.0,
        test_cases=[
            cs.TestCase(
                execution_order=order,
                input_path="test-uncompilable/stdin.txt",
                expected_output_path="test-uncompilable/expected.txt",
            )
            for order in (0, 1)
        ],
        evaluator_path=_write_evaluator(
            storage, "broken.c", "int main() { asdfasdfg; }\n"
        ),
    )
    res = await cj.run_submission(storage, run_data)
    assert [o.outcome for o in res] == [cs.OutcomeEnum.evaluator_error] * 2
    assert all("failed to compile" in (o.message or "") for o in res)


async def test_missing_evaluator_is_reported_not_raised(tmp_path: Path):
    """No isolate needed: the file is gone before anything can be compiled."""
    with pytest.raises(cj.EvaluatorError, match="missing from storage"):
        await cj.prepare_evaluator(
            Storage(tmp_path),
            "nowhere/checker.py",
            cj.load_languages(),
            tmp_path,
        )


async def test_evaluator_in_unknown_language_is_reported(tmp_path: Path):
    storage = Storage(tmp_path)
    storage.write("checkers/checker.txt", "not a program")
    with pytest.raises(cj.EvaluatorError, match="no language"):
        await cj.prepare_evaluator(
            storage,
            "checkers/checker.txt",
            cj.load_languages(),
            tmp_path,
        )



########################################################################
# Feeding a compressed input to a box's standard input. No isolate here: the
# feeder is the one genuinely new mechanism in the run path, and pinning it
# against a plain subprocess is what makes it testable at all - every test that
# goes through `isolate_run` needs a sandbox and a nix store.


async def _feed_to(command: list[str], path: Path) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    # Read the output alongside the feeder rather than through `communicate`,
    # which is what `isolate_run` does and for its reason: given a stdin pipe,
    # that method closes it whether or not it was given anything to write, so
    # the child would see EOF while the feeder was still inflating into it.
    stdout, _ = await asyncio.gather(
        proc.stdout.read(), cj._feed_stdin(proc.stdin, path)
    )
    await proc.wait()
    return stdout


async def test_a_compressed_input_arrives_whole(tmp_path: Path):
    """A million lines, which is the case this exists for: it goes in
    compressed and comes out byte for byte, without ever being written out."""
    contents = "".join(f"{i} {i * 2}\n" for i in range(200_000)).encode()
    path = tmp_path / "in_0.txt"
    with gzip.open(path, "wb") as fp:
        fp.write(contents)
    assert path.stat().st_size < len(contents) // 3

    assert await _feed_to(["cat"], path) == contents


async def test_a_program_that_stops_reading_early_is_not_an_error(tmp_path: Path):
    """The ordinary shape of a solution that reads one number off a huge input,
    and of every submission that times out. The broken pipe belongs to the
    feeder, and the box's own status is what says how the run ended - so this
    must neither raise nor hang."""
    path = tmp_path / "in_0.txt"
    with gzip.open(path, "wb") as fp:
        fp.write(b"x" * (8 << 20))

    out = await asyncio.wait_for(_feed_to(["head", "-c", "10"], path), timeout=30)
    assert out == b"x" * 10


async def test_an_empty_input_is_an_empty_stdin(tmp_path: Path):
    path = tmp_path / "in_0.txt"
    with gzip.open(path, "wb") as fp:
        fp.write(b"")

    assert await _feed_to(["cat"], path) == b""


async def test_a_graded_run_logs_with_its_submission_bound(monkeypatch, caplog):
    """Every line emitted underneath `run_from_queue` carries the submission,
    without the line naming it. No sandbox: the grading itself is replaced by
    a function that only logs, and so are the two database writes."""
    import contextlib
    import logging

    import cocco.schema as cs
    from cocco.logs import ContextFilter, record_fields

    run_data = cs.SubmissionRunData(
        submission_id=812,
        challenge_id=12,
        language="python",
        source="",
        filename="a.py",
        max_threads=1,
        max_memory_bytes=1 << 26,
        max_time_s=1.0,
        test_cases=[],
    )

    async def pull(conn):
        return run_data

    async def grade(storage, data):
        logging.getLogger("cocco.judge").info("deep inside the run")
        return []

    class Engine:
        def connect(self):
            return contextlib.nullcontext()

        begin = connect

    monkeypatch.setattr(cj.repo, "pull_queued_submission", pull)
    monkeypatch.setattr(cj.repo, "insert_test_outcomes", lambda conn, o: None)
    monkeypatch.setattr(cj.repo, "mark_submission_done", lambda conn, i: None)
    monkeypatch.setattr(cj, "run_submission", grade)

    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.INFO, logger="cocco.judge"):
        assert await cj.run_from_queue(Engine(), None)

    [inside] = [r for r in caplog.records if r.getMessage() == "deep inside the run"]
    fields = record_fields(inside)
    assert fields["submission_id"] == 812
    assert fields["challenge_id"] == 12
    assert fields["language"] == "python"


########################################################################
# the run loop: workers, the wake-up and the priority


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_queues_are_asked_in_priority_order(monkeypatch):
    """A free worker takes the first queue that has work, so nothing on a
    later one starts while an earlier one has some."""
    asked = []

    def queue(name: str, has_work: bool):
        async def take(engine, storage):
            asked.append(name)
            return has_work

        return take

    monkeypatch.setattr(
        cj, "QUEUES", (queue("first", False), queue("second", True), queue("third", True))
    )
    assert await cj.run_next(None, None)
    assert asked == ["first", "second"]


async def test_the_workers_run_concurrently_up_to_the_box_pool(monkeypatch):
    """More workers than boxes: the workers really do overlap - the pool ends
    up full, not one box at a time - and the pool, not the worker count, is
    what bounds the sandboxes open at once."""
    monkeypatch.setattr(cj, "_IDS_POOL", cj.BoxPool(3))
    remaining = 24
    held: set[int] = set()
    reused: list[int] = []
    peak = 0
    finished = 0
    drained = asyncio.Event()

    async def take(engine, storage):
        nonlocal remaining, peak, finished
        if not remaining:
            return False
        remaining -= 1
        async with cj._IDS_POOL.acquire() as box_id:
            # Recorded rather than asserted: a worker logs and survives an
            # exception, so an assert here would only show up as a timeout.
            if box_id in held:
                reused.append(box_id)
            held.add(box_id)
            peak = max(peak, len(held))
            await asyncio.sleep(0.01)
            held.discard(box_id)
        finished += 1
        if finished == 24:
            drained.set()
        return True

    async def no_listener(engine, wakeup, retry_s):
        await asyncio.Event().wait()

    monkeypatch.setattr(cj, "QUEUES", (take,))
    monkeypatch.setattr(cj, "listen_for_work", no_listener)

    loop = asyncio.create_task(cj.run_loop(None, None, concurrency=8, wait_time=60))
    try:
        await asyncio.wait_for(drained.wait(), 5)
    finally:
        await _cancel(loop)
    assert reused == []
    assert peak == 3


async def test_a_failing_job_does_not_take_its_worker_down(monkeypatch, caplog):
    """The failure is logged and the worker carries on, rather than the whole
    judge stopping with every other worker's submission half graded."""
    calls = 0
    recovered = asyncio.Event()

    async def take(engine, storage):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        recovered.set()
        return False

    async def no_listener(engine, wakeup, retry_s):
        await asyncio.Event().wait()

    monkeypatch.setattr(cj, "QUEUES", (take,))
    monkeypatch.setattr(cj, "listen_for_work", no_listener)

    loop = asyncio.create_task(cj.run_loop(None, None, concurrency=1, wait_time=0.05))
    try:
        await asyncio.wait_for(recovered.wait(), 5)
    finally:
        await _cancel(loop)
    assert any("boom" in (r.exc_text or "") for r in caplog.records)


async def test_sigterm_stops_the_judge_by_cancelling_it(monkeypatch):
    """systemd's stop is a cancellation, so every worker gets to put its
    submission back and clean its boxes up, instead of the process dying with
    them `running`."""
    import os
    import signal

    started = asyncio.Event()
    cancelled = False

    async def run_loop(engine, storage, concurrency):
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(cj, "run_loop", run_loop)

    async def serve():
        # `_serve` installs its handler on the task it runs in, as it does on
        # `asyncio.run`'s main task.
        await cj._serve(None, None, 1)

    task = asyncio.create_task(serve())
    try:
        await asyncio.wait_for(started.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
    assert cancelled


def test_a_ring_between_the_look_and_the_sleep_is_not_lost():
    """The ticket is taken before the queue is looked at, so a notification
    that arrives after the look has already ended the sleep that follows it."""
    wakeup = cj.Wakeup()
    ticket = wakeup.ticket()
    wakeup.ring()
    assert ticket.is_set()
    assert not wakeup.ticket().is_set()


def _user_and_challenge(db) -> tuple[int, int]:
    with db.begin() as conn:
        image, image_mime = generate_identicon("student")
        user_id = conn.execute(
            insert(cm.User)
            .values(email="s@unipd.it", nickname="s", image=image, image_mime=image_mime)
            .returning(cm.User.c.id)
        ).scalar_one()
        challenge_id = conn.execute(
            insert(cm.Challenge)
            .values(title="Hi", description="", max_memory_bytes=1 << 20, max_time_s=1.0)
            .returning(cm.Challenge.c.id)
        ).scalar_one()
    return user_id, challenge_id


def _listener_pids(db) -> list[int]:
    """The backends listening on anything, which in a test is the judge's."""
    with db.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE datname = current_database() AND query LIKE 'LISTEN %'"
                )
            ).scalars()
        )


async def _until(predicate, timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def _status(db, submission_id: int):
    with db.connect() as conn:
        return conn.execute(
            select(cm.Submission.c.status, cm.Submission.c.started_at).where(
                cm.Submission.c.id == submission_id
            )
        ).one()


async def _enqueue(app_engine, user_id: int, challenge_id: int) -> int:
    """A submission, the way `post_submission` makes one - notification and all."""
    async with app_engine.connect() as conn:
        submission_id = await insert_submission(
            conn,
            user_id=user_id,
            challenge_id=challenge_id,
            language="python",
            filename="a.py",
            source="",
        )
        await conn.commit()
    return submission_id


async def test_a_notification_wakes_an_idle_judge(db, app_engine, monkeypatch):
    """A submission committed through the web app's own `insert_submission`
    is picked up far sooner than the poll interval - here a minute - because
    the commit notifies the judge. No sandbox: the grading is replaced."""

    async def grade(storage, run_data):
        return []

    monkeypatch.setattr(cj, "run_submission", grade)
    user_id, challenge_id = _user_and_challenge(db)

    loop = asyncio.create_task(cj.run_loop(db, None, concurrency=2, wait_time=60))
    try:
        # Idle: both workers have looked, found nothing, and gone to sleep for
        # the minute, and the listener is on the channel.
        await _until(lambda: _listener_pids(db))
        await asyncio.sleep(0.1)

        start = asyncio.get_running_loop().time()
        submission_id = await _enqueue(app_engine, user_id, challenge_id)
        await _until(
            lambda: _status(db, submission_id).status == cm.SubmissionStatus.done
        )
        elapsed = asyncio.get_running_loop().time() - start
    finally:
        await _cancel(loop)

    assert elapsed < 1


async def test_the_listener_comes_back_after_losing_its_connection(db, app_engine):
    """A dropped channel costs latency until the reconnect, and nothing else."""
    user_id, challenge_id = _user_and_challenge(db)
    wakeup = cj.Wakeup()
    listener = asyncio.create_task(cj.listen_for_work(db, wakeup, retry_s=0.05))
    try:
        await _until(lambda: _listener_pids(db))
        [pid] = _listener_pids(db)
        with db.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await _until(lambda: _listener_pids(db) not in ([], [pid]))

        ticket = wakeup.ticket()
        await _enqueue(app_engine, user_id, challenge_id)
        await asyncio.wait_for(ticket.wait(), 5)
    finally:
        await _cancel(listener)


async def test_a_judge_stopped_mid_run_puts_the_submission_back(db, app_engine, monkeypatch):
    """Cancelled while grading - which is what SIGTERM now is - the submission
    goes back to `queued` with no start time, rather than staying `running`
    and refusing every regrade and delete of its challenge."""
    grading = asyncio.Event()

    async def grade(storage, run_data):
        grading.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cj, "run_submission", grade)
    user_id, challenge_id = _user_and_challenge(db)
    submission_id = await _enqueue(app_engine, user_id, challenge_id)

    run = asyncio.create_task(cj.run_from_queue(db, None))
    await asyncio.wait_for(grading.wait(), 5)
    assert _status(db, submission_id).status == cm.SubmissionStatus.running

    await _cancel(run)
    assert tuple(_status(db, submission_id)) == (cm.SubmissionStatus.queued, None)


@pytest.mark.integration
async def test_a_cancelled_run_cleans_its_box_up():
    """A box whose run is cancelled is cleaned up and its id handed back, so
    the next `--init` on it finds nothing left behind."""
    python = await cj.resolve_runtime("python3", "python")
    pool = cj.BoxPool(1)
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "spin.py"
        script.write_text("while True:\n    pass\n")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cj, "_IDS_POOL", pool)
            run = asyncio.create_task(
                cj.isolate_run(
                    python, None, ["spin.py"], time_limit=30,
                    box_files={"spin.py": script},
                )
            )
            await asyncio.sleep(1)
            await _cancel(run)

            hello = Path(tmp) / "hello.py"
            hello.write_text("print('again')\n")
            res = await asyncio.wait_for(
                cj.isolate_run(
                    python, None, ["hello.py"], box_files={"hello.py": hello}
                ),
                10,
            )
    assert res.stdout.strip() == "again"


_COLLECT_PROGRAM = textwrap.dedent("""\
    import os
    open("main", "w").write("binary")
    open("A.class", "w").write("a")
    open("B$Inner.class", "w").write("b")
    open("other.txt", "w").write("other")
    print("printed")
""")


async def _collect_run(tmp: Path, program: str, collect: list[str], **kwargs):
    python = await cj.resolve_runtime("python3", "python")
    script = tmp / "prog.py"
    script.write_text(program)
    out = tmp / "out"
    res = await cj.isolate_run(
        python, None, ["prog.py"], box_files={"prog.py": script},
        collect=collect, collect_into=out, **kwargs,
    )
    return res, out


def _collected(out: Path) -> set[str]:
    return {p.name for p in out.iterdir()} if out.exists() else set()


@pytest.mark.integration
async def test_collect_copies_a_named_file_and_a_glob_and_nothing_else(tmp_path):
    res, out = await _collect_run(tmp_path, _COLLECT_PROGRAM, ["main", "*.class"])
    assert res.stdout.strip() == "printed"
    assert _collected(out) == {"main", "A.class", "B$Inner.class"}
    assert (out / "main").read_text() == "binary"


@pytest.mark.integration
async def test_collect_does_not_follow_a_symlink(tmp_path):
    secret = tmp_path / "secret.txt"
    marker = "HOST-SECRET-7f3a9c1e"
    secret.write_text(marker)
    program = f"import os\nos.symlink({str(secret)!r}, 'hello')\nopen('real', 'w').write('ok')\n"
    res, out = await _collect_run(tmp_path, program, ["hello", "real"])
    assert _collected(out) == {"real"}
    assert all(marker not in p.read_text() for p in out.iterdir())


@pytest.mark.integration
async def test_collect_skips_directories_fifos_and_the_judges_own_files(tmp_path):
    program = textwrap.dedent("""\
        import os
        os.mkdir("adir")
        os.mkfifo("afifo")
        print("loud")
    """)
    res, out = await _collect_run(
        tmp_path, program, ["adir", "afifo", "_stdout", "_stderr", "_*"]
    )
    assert res.stdout.strip() == "loud"
    assert _collected(out) == set()


@pytest.mark.integration
async def test_collect_matching_nothing_collects_nothing(tmp_path):
    res, out = await _collect_run(tmp_path, "print('x')\n", ["nope", "*.class"])
    assert res.status == "OK"
    assert _collected(out) == set()


def test_collect_over_the_size_limit_raises_rather_than_truncating(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    (box / "big").write_bytes(b"x" * 2048)
    with pytest.raises(RuntimeError, match="limit"):
        cj._collect_from_box(box, ["big"], tmp_path / "out", 1)
    cj._collect_from_box(box, ["big"], tmp_path / "out", 2)
    assert (tmp_path / "out" / "big").stat().st_size == 2048


async def test_collect_refuses_patterns_that_could_leave_the_box(tmp_path):
    python = Path("python")
    for bad in ["../x", "a/b", "..", "/etc/passwd"]:
        with pytest.raises(ValueError):
            await cj.isolate_run(python, None, [], collect=[bad], collect_into=tmp_path)
    with pytest.raises(ValueError):
        await cj.isolate_run(python, None, [], collect=["x"])
