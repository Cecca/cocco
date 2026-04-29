from contextlib import contextmanager
from pathlib import Path
import shutil


class Storage(object):
    """
    Handle storage necessities by abstracting the base directory.
    """

    basedir: Path

    def __init__(self, basedir: Path):
        self.basedir = basedir
        if not self.basedir.is_dir():
            self.basedir.mkdir(parents=True)

    @contextmanager
    def open(self, path: Path | str, mode="r"):
        with open(self.resolve(path), mode) as fp:
            yield fp

    def write(self, path: Path | str, contents: str):
        """convenience method to write a file"""
        path = self.resolve(path)
        if not path.parent.is_dir():
            path.parent.mkdir(parents=True)
        with self.open(path, "w") as fp:
            fp.write(contents)

    def read(self, path: Path | str) -> str:
        with self.open(path, "r") as fp:
            return fp.read()

    def resolve(self, path: Path | str) -> Path:
        return self.basedir / path

    def copy(self, path_from: Path, path_to: Path):
        shutil.copy(self.resolve(path_from), self.resolve(path_to))
