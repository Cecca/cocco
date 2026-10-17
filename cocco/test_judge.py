import asyncio
import gzip
import tempfile
import textwrap
from pathlib import Path

import pytest

import cocco.judge as cj
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
            [str(script_path)]
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
