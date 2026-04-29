import tempfile
import textwrap
from pathlib import Path

import pytest
from icecream import ic

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
    python = cj.load_languages(Path("languages.toml"))["python"]
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
    c = cj.load_languages(Path("languages.toml"))["c"]
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
    cpp = cj.load_languages(Path("languages.toml"))["cpp"]
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
    python = cj.load_languages(Path("languages.toml"))["java"]
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
    assert cj.EqualityEvaluator().evaluate("hello moon", res.stdout)[0] == 1


def test_evaluator():
    evaluator = cj.DiffEvaluator()
    ev = evaluator.evaluate("one\ntwo\nthree\nfour", "one\nfour\nthree\nfour")
    print(ev[1])
    assert ev[0] == 0.75


async def test_run_submission():
    import cocco.model as cm

    storage = Storage(Path("/tmp/cocco-storage"))

    challenge = cm.Challenge(
        title="Hello",
        description="Print hello <name>",
        max_memory_bytes=64 * 1024 * 1024,
        max_time_s=1.0,
    )
    tests = []
    stdin_path = Path("test-run-from-queue/stdin.txt")
    expected_path = Path("test-run-from-queue/expected.txt")
    storage.write(stdin_path, "moon")
    storage.write(expected_path, "hello moon")
    assert storage.resolve(stdin_path).is_file()
    assert storage.resolve(expected_path).is_file()
    tests.append(
        cm.TestCase(
            challenge_id=challenge.id,
            execution_order=0,
            input_path=str(stdin_path),
            expected_output_path=str(expected_path),
        )
    )

    # correct submission
    submission = cm.Submission(
        challenge_id=challenge.id,
        language="python",
        source='print("hello", input())',
    )
    res = await cj.run_submission(storage, cj.EqualityEvaluator(), submission, challenge, tests)
    assert res[0].outcome == cm.OutcomeEnum.success
    assert res[0].score == 1

    # failing submission, by its score
    submission = cm.Submission(
        challenge_id=challenge.id,
        language="python",
        source='print("wrong!", input())',
    )
    res = await cj.run_submission(storage, cj.EqualityEvaluator(), submission, challenge, tests)
    assert res[0].outcome == cm.OutcomeEnum.success
    assert res[0].score == 0

    # submission that crashes
    submission = cm.Submission(
        challenge_id=challenge.id,
        language="python",
        source='import sys; sys.exit(127)',
    )
    res = await cj.run_submission(storage, cj.EqualityEvaluator(), submission, challenge, tests)
    assert res[0].outcome == cm.OutcomeEnum.failure
    assert res[0].score == 0

    # submission that does not compile
    submission = cm.Submission(
        challenge_id=challenge.id,
        language="c",
        source="""
            int main() {
                asdfasdfg; // <- this does not compile
                return 0;
            }
        """,
    )
    res = await cj.run_submission(storage, cj.EqualityEvaluator(), submission, challenge, tests)
    assert res[0].outcome == cm.OutcomeEnum.compile_failure
    assert res[0].score == 0


# async def test_run_from_queue():
#     from sqlmodel import Session, SQLModel, create_engine, select
#     import cocco.model as cm

#     storage = Storage(Path("/tmp/cocco-storage"))
#     engine = create_engine("sqlite:///:memory:", echo=False)
#     SQLModel.metadata.create_all(engine)
#     session = Session(engine)

#     res = await cj.run_from_queue(session, storage, cj.EqualityEvaluator())
#     assert res is None

#     user = cm.User(
#         email="student@example.com",
#         role=cm.UserRole.student,
#         nickname="happy-otter",
#     )
#     challenge = cm.Challenge(
#         title="Hello",
#         description="Print hello <name>",
#         max_memory_bytes=64 * 1024 * 1024,
#         max_time_s=1.0,
#     )
#     session.add(user)
#     session.add(challenge)
#     session.commit()
#     session.refresh(user)
#     session.refresh(challenge)

#     stdin_path = Path("test-run-from-queue/stdin.txt")
#     expected_path = Path("test-run-from-queue/expected.txt")
#     storage.write(stdin_path, "moon")
#     storage.write(expected_path, "hello moon")
#     assert storage.resolve(stdin_path).is_file()
#     assert storage.resolve(expected_path).is_file()
#     testcase = cm.TestCase(
#         challenge_id=challenge.id,
#         execution_order=0,
#         input_path=str(stdin_path),
#         expected_output_path=str(expected_path),
#     )
#     submission = cm.Submission(
#         user_id=user.id,
#         challenge_id=challenge.id,
#         language="python",
#         source='print("hello", input())',
#     )
#     session.add(testcase)
#     session.add(submission)
#     session.commit()
#     session.refresh(submission)
#     session.commit()

#     queued = session.exec(select(cm.Submission)).all()
#     assert len(queued) == 1
#     assert queued[0].status == cm.SubmissionStatus.queued

#     res = await cj.run_from_queue(session, storage, cj.EqualityEvaluator())
#     assert res is None
