import tempfile
import textwrap
from pathlib import Path

import pytest

import cocco.judge as cj
from cocco.storage import Storage


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
        stdin_path = tmp / "stdin.txt"
        with open(stdin_path, "w") as fp:
            fp.write("moon")
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
        stdin_path = tmp / "stdin.txt"
        with open(stdin_path, "w") as fp:
            fp.write("moon")

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
        stdin_path = tmp / "stdin.txt"
        with open(stdin_path, "w") as fp:
            fp.write("moon")

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
        stdin_path = tmp / "stdin.txt"
        with open(stdin_path, "w") as fp:
            fp.write("moon")

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
        stdin_path = tmp / "stdin.txt"
        with open(stdin_path, "w") as fp:
            fp.write("moon")

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
        language: str, source: str, evaluator_path: str | None = None
    ) -> cs.SubmissionRunData:
        return cs.SubmissionRunData(
            submission_id=1,
            language=language,
            source=source,
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
    )
    res = await cj.run_submission(storage, run_data)
    assert res[0].outcome == cs.OutcomeEnum.compile_failure
    assert res[0].score == 0


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

