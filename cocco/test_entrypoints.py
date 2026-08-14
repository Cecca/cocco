"""The four console scripts, checked as what they are: importable names.

`[project.scripts]` is a string in `pyproject.toml`, so a target that has been
renamed, moved or - the actual bug this file exists for - defined *inside* a
`if __name__ == "__main__":` block is a typo nothing catches until somebody
installs the wheel and runs the command. `cocco-judge`'s `main` was an
`async def` in exactly that position, which `python -m cocco.judge` runs
happily and an entry point cannot name at all.

Importing `cocco.server` is deliberately cheap - it is `argparse` and
`uvicorn`, and pointedly *not* `cocco.main`, whose import builds an engine and
creates the storage root. That laziness is the whole reason `--config` can
work, so a test that imports it and does not pay for a database is also the
test that it stayed lazy.
"""

import importlib
import subprocess
import sys

import pytest

# module, attribute, the name `pyproject.toml` gives it
ENTRY_POINTS = [
    ("cocco.cli", "main", "cocco"),
    ("cocco.server", "main", "cocco-web"),
    ("cocco.judge", "main", "cocco-judge"),
    ("cocco.migrate", "main", "cocco-migrate"),
]


@pytest.mark.parametrize("module_name, attribute, script", ENTRY_POINTS)
def test_the_entry_point_is_importable(module_name, attribute, script):
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute)), (
        f"{script} = {module_name}:{attribute} in pyproject.toml names "
        f"something that is not a function"
    )


@pytest.mark.parametrize("module_name, attribute, script", ENTRY_POINTS)
def test_the_entry_point_takes_argv(module_name, attribute, script):
    """Each one takes `argv` rather than reaching for `sys.argv`, which is
    what makes it testable - and what let the judge's parser move out of the
    `__main__` guard without changing a single flag."""
    module = importlib.import_module(module_name)
    with pytest.raises(SystemExit) as exit_code:
        getattr(module, attribute)(["--help"])
    assert exit_code.value.code == 0


def test_the_server_does_not_import_the_app():
    """`cocco.server` sets COCCO_CONFIG and *then* hands uvicorn the import
    string "cocco.main:app". If importing it pulled `cocco.main` in, the
    configuration would already have been read by the time the flag was
    parsed, and the flag would silently do nothing.

    In a subprocess, because the question is about a *fresh* interpreter:
    half the test suite imports `cocco.main` at module level, and clearing it
    out of `sys.modules` here would leave those modules holding a stale app.
    """
    probe = (
        "import sys, cocco.server; "
        "sys.exit('cocco.main' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0
