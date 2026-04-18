import textwrap
import tempfile
from pathlib import Path
from icecream import ic

import pytest

import cocco.judge as cj


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
        res = await python.run(exe, stdin_path)
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
        res = await c.run(exe, stdin_path)
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
        res = await cpp.run(exe, stdin_path)
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
        res = await python.run(exe, stdin_path)
    assert res.stdout.strip() == "hello moon"
    assert cj.EqualityEvaluator("hello moon").evaluate(res.stdout)[0] == 1


def test_evaluator():
    evaluator = cj.DiffEvaluator("one\ntwo\nthree\nfour")
    ev = evaluator.evaluate("one\nfour\nthree\nfour")
    print(ev[1])
    assert ev[0] == 0.75
