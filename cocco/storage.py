from contextlib import contextmanager
from pathlib import Path
import re
import shutil

# Conservative: what is left after this must still be a usable file name on any
# filesystem, and must not be able to reach outside a directory.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
MAX_FILENAME_LENGTH = 120


def safe_filename(name: str) -> str:
    """Reduce a user-supplied name to a bare, harmless file name.

    Raises ValueError rather than silently substituting something, so that a
    caller cannot accidentally write to a path the user did not ask for.
    """
    # Browsers may send a full path in the multipart filename
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _UNSAFE.sub("_", name)
    name = name.lstrip(".")
    if not name or name in (".", ".."):
        raise ValueError("invalid file name")
    if len(name) > MAX_FILENAME_LENGTH:
        raise ValueError(f"file name longer than {MAX_FILENAME_LENGTH} characters")
    return name


class Storage(object):
    """
    Handle storage necessities by abstracting the base directory.
    """

    basedir: Path

    def __init__(self, basedir: Path):
        self.basedir = basedir.resolve()
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
        """Absolute path inside the storage root.

        Test case paths come from the database and, once teachers can name
        files, ultimately from user input. `basedir / path` alone is a path
        traversal hole: an absolute path replaces the base entirely and `..`
        walks out of it. Anything that does not land under the root is refused.
        """
        resolved = (self.basedir / path).resolve()
        if resolved != self.basedir and not resolved.is_relative_to(self.basedir):
            raise ValueError(f"path escapes the storage root: {path}")
        return resolved

    def copy(self, path_from: Path, path_to: Path):
        shutil.copy(self.resolve(path_from), self.resolve(path_to))

    def exists(self, path: Path | str) -> bool:
        return self.resolve(path).is_file()

    def size(self, path: Path | str) -> int:
        return self.resolve(path).stat().st_size

    def delete(self, path: Path | str) -> None:
        """Remove a file. Missing files are not an error: the caller is usually
        deleting a database row whose file may already be gone."""
        self.resolve(path).unlink(missing_ok=True)

    def delete_directory(self, path: Path | str) -> None:
        """Remove a directory, but only once it is already empty.

        `rmdir` and deliberately never `shutil.rmtree`. The caller has just
        unlinked the files it had rows for; anything still in there is
        something this application either did not put there or lost the row
        for, and destroying it on the way past is exactly the mistake this
        class exists to prevent. So a non-empty directory is left alone and is
        a thing to look at rather than a thing to lose, and a missing one is
        not an error for `delete`'s reason.

        Refusing the root is not paranoia: `resolve("")` *is* the storage root
        and `resolve` allows it, by its own first branch.
        """
        target = self.resolve(path)
        if target == self.basedir:
            raise ValueError("refusing to remove the storage root")
        try:
            target.rmdir()
        except OSError:
            pass

    def rename(self, path_from: Path | str, path_to: Path | str) -> None:
        target = self.resolve(path_to)
        if target.exists():
            raise ValueError(f"{path_to} already exists")
        if not target.parent.is_dir():
            target.parent.mkdir(parents=True)
        self.resolve(path_from).rename(target)

    def save_stream(self, path: Path | str, fileobj) -> int:
        """Stream an open file object to `path`, returning the bytes written.

        Test case inputs can be several megabytes (see DESIGN.md), so they are
        copied straight to disk rather than read into memory.
        """
        target = self.resolve(path)
        if not target.parent.is_dir():
            target.parent.mkdir(parents=True)
        with open(target, "wb") as fp:
            shutil.copyfileobj(fileobj, fp)
        return target.stat().st_size
