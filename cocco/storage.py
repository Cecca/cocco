from contextlib import contextmanager
from pathlib import Path
import shutil


class Storage(object):
    """
    Handle storage necessities by abstracting away the filesystem.
    """

    basedir: Path

    def __init__(self, basedir: Path):
        self.basedir = basedir

    @contextmanager
    def open(self, path: Path, mode="r"):
        with open(self.basedir / path, mode) as fp:
            yield fp

    def copy(self, path_from: Path, path_to: Path):
        shutil.copy(self.basedir / path_from, self.basedir / path_to)

            
    

