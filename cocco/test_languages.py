import re
import textwrap

import pytest

from cocco.languages import LanguageSpec, load_languages


def _load(tmp_path, body: str):
    path = tmp_path / "languages.toml"
    path.write_text(textwrap.dedent(body))
    return load_languages(path)


def test_the_optional_keys_default_to_nothing(tmp_path):
    lang = _load(tmp_path, '[x]\nextensions = ["x"]\nruntime = "x"\n')["x"]
    assert lang.env == {} and lang.error_name is None and lang.editor_mode is None


def test_the_bundled_entries_carry_their_keys():
    languages = load_languages()
    assert languages["python"].env == {"PYTHONHASHSEED": "0"}
    assert languages["python"].editor_mode == "python"
    assert languages["c"].editor_mode == "text/x-csrc"
    assert languages["c"].error_name is None
    assert languages["c"].compiler_args[-1] == "-lm"
    assert languages["cpp"].editor_mode == "text/x-c++src"
    assert languages["java"].editor_mode == "text/x-java"


def test_python_error_name_reads_the_last_traceback_name():
    pattern = re.compile(load_languages()["python"].error_name)
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "main.py", line 1, in <module>\n'
        "    int('x')\n"
        "ValueError: invalid literal\n"
    )
    assert pattern.findall(stderr)[-1] == "ValueError"
    assert not pattern.search("bye\n")


@pytest.mark.parametrize(
    "line",
    ['env = { A = 1 }', 'error_name = "("', "editor_mode = 3", 'error_name = 3'],
)
def test_bad_optional_keys_are_refused(tmp_path, line):
    with pytest.raises(ValueError):
        _load(tmp_path, f'[x]\nextensions = ["x"]\nruntime = "x"\n{line}\n')


def test_the_judge_builds_executable_languages():
    import cocco.judge as cj

    language = cj.load_languages()["python"]
    assert isinstance(language, cj.Language) and isinstance(language, LanguageSpec)


@pytest.mark.integration
async def test_run_applies_env_and_fsize(tmp_path):
    import cocco.judge as cj

    python = cj.load_languages()["python"]
    src = tmp_path / "main.py"
    src.write_text(
        "import os\nprint(os.environ['PYTHONHASHSEED'])\n"
        "with open('big', 'w') as f:\n    f.write('x' * 4096)\n"
    )
    res = await python.run(src, None, fsize_kb=1)
    assert res.stdout.strip() == "0"
    # the 1 KB cap is isolate's, so the 4 KB write fails
    assert res.status != "OK"
