import textwrap
import asyncio
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
                    Scanner s = new Scanner(System.in);
                    System.out.println("hello " + scanner.next());
                }
            }
            """))
        stdin_path = tmp / "stdin.txt"
        with open(stdin_path, "w") as fp:
            fp.write("moon")

        exe = await python.compile(script_path, tmp)
        res = await python.run(exe, stdin_path)
        print(res.stdout)
    assert res.stdout.strip() == "hello moon"
