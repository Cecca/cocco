"""What `languages.toml` says about each language, with no execution in it.

The web process and the exercise format read names, extensions and the editor
mode, and neither should import the judge to do it (it pulls in isolate, nix
and the database). So the plain description lives here, and `cocco.judge`
subclasses `LanguageSpec` with `compile` / `run`, which need its internals.
`load_languages(cls=...)` builds whichever of the two the caller wants.
"""

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, TypeVar

logger = logging.getLogger("cocco.languages")

# Resolved relative to this module and never to the working directory, the same
# rule `cocco/evaluators/` states for the bundled scripts. It lives *inside*
# `cocco/` rather than beside it, which is what lets an installed `cocco-judge`
# find it: a wheel has a package directory and no checkout.
LANGUAGES_PATH = Path(__file__).resolve().parent / "languages.toml"


@dataclass
class LanguageSpec:
    """One entry of `languages.toml`.

    `compiler` / `interpreter` pick the shape (interpreted, compiled native,
    compiled + VM); `cocco.judge.Language` documents the placeholders.

    Optional keys that only matter to exercises and the editor:
    - `env`: variables set in the run box (python's `PYTHONHASHSEED`, so a
      `set` prints in the same order every run).
    - `error_name`: a regex searched in stderr of a program that exited
      non-zero, last match winning, group 1 naming the error.
    - `editor_mode`: CodeMirror's mode or MIME for this language.
    """

    name: str
    extensions: List[str]
    runtime: str
    compiler: str | None = None
    compiler_args: List[str] = field(default_factory=list)
    output_ext: str = ""
    interpreter: str | None = None
    interpreter_args: List[str] = field(default_factory=list)
    additional_processes: int = 0
    env: Dict[str, str] = field(default_factory=dict)
    error_name: str | None = None
    editor_mode: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.env.items()
        ):
            raise ValueError(f"language {self.name}: env must be a table of strings")
        if self.error_name is not None:
            if not isinstance(self.error_name, str):
                raise ValueError(f"language {self.name}: error_name must be a string")
            try:
                re.compile(self.error_name)
            except re.error as e:
                raise ValueError(
                    f"language {self.name}: error_name is not a regex: {e}"
                ) from e
        if self.editor_mode is not None and not isinstance(self.editor_mode, str):
            raise ValueError(f"language {self.name}: editor_mode must be a string")

    @property
    def is_interpreted(self) -> bool:
        return self.interpreter is not None


L = TypeVar("L", bound=LanguageSpec)


def load_languages(
    path: Path = LANGUAGES_PATH, cls: type[L] = LanguageSpec  # type: ignore[assignment]
) -> Dict[str, L]:
    with open(path, "rb") as fp:
        data = tomllib.load(fp)
    languages = {name: cls(name=name, **cfg) for name, cfg in data.items()}
    logger.debug("loaded %d languages from %s: %s", len(languages), path, list(languages))
    return languages


def extension_to_language(languages: Dict[str, L]) -> Dict[str, str]:
    """Extension (no dot, lowercased) -> language name."""
    return {
        ext.lower(): name
        for name, language in languages.items()
        for ext in language.extensions
    }


def language_for_filename(filename: str, languages: Dict[str, L]) -> L | None:
    """The language a file name implies, or None if the extension is unknown.

    The same rule for a submitted solution and for an uploaded evaluator: the
    extension is the only thing that says what a file is, which is why neither
    carries a language field.
    """
    if "." not in filename:
        return None
    extension = filename.rsplit(".", 1)[-1].lower()
    name = extension_to_language(languages).get(extension)
    return languages[name] if name is not None else None
