from contextlib import contextmanager
from pathlib import Path
import gzip
import os
import re
import shutil

# Conservative: what is left after this must still be a usable file name on any
# filesystem, and must not be able to reach outside a directory.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
MAX_FILENAME_LENGTH = 120

# Everything under the storage root is a gzip stream stored under its logical
# name - `challenge_7/input_01.in` is what the database says and what the
# transfer format calls it, and the bytes at that path are compressed. There is
# deliberately no `.gz` suffix: a suffix would have to be appended by every
# writer and stripped by every reader, and `resolve`, `exists`, `rename`,
# `delete`, the rename route and `plan_testcases` all key on the name. Only the
# codec lives here; the name is the same one it always was.
GZIP_LEVEL = 6
_GZIP_MAGIC = b"\x1f\x8b"
_CHUNK = 1 << 20

# The root says what format it is in, the way `alembic_version` says which
# revision a database is at. The leading dot is what makes it unclaimable, for
# the reason the judge's `.expected_output` is: `safe_filename` strips leading
# dots, so no upload can ever be named this.
FORMAT_MARKER = ".storage-format"
FORMAT_GZIP = "gzip"

# What to tell somebody whose storage root predates all of this.
COMPRESS_COMMAND = "cocco-migrate compress-storage"


class StorageFormatError(RuntimeError):
    """The storage root, or one file in it, is not in the format this version
    reads. Always a deployment that has not run `cocco-migrate
    compress-storage`, never a per-request problem, so it names the command."""


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


def _copy(source, sink) -> int:
    """`shutil.copyfileobj` that says how much it moved."""
    copied = 0
    while chunk := source.read(_CHUNK):
        sink.write(chunk)
        copied += len(chunk)
    return copied


def is_compressed(path: Path) -> bool:
    """Whether `path` begins with the gzip magic. Two bytes, not a decompression:
    this is asked once per file by the migration, and per read as a guard."""
    with open(path, "rb") as fp:
        return fp.read(2) == _GZIP_MAGIC


def _require_compressed(path: Path) -> None:
    """`gzip` only notices a plaintext file on the first read, which on a
    multi-megabyte input is halfway through grading a submission. Ask up front,
    and say what to run."""
    if not is_compressed(path):
        raise StorageFormatError(
            f"{path} is not compressed. This deployment's storage root predates "
            f"compressed storage; run `{COMPRESS_COMMAND}`."
        )


@contextmanager
def open_plaintext(path: Path):
    """A compressed file on an absolute path, read back as plaintext bytes.

    A module function on a `Path` rather than a `Storage` method, so that
    `judge.py`'s sandbox primitive can decompress without holding a store.
    """
    _require_compressed(path)
    with gzip.open(path, "rb") as fp:
        yield fp


def expand(src: Path, dest: Path) -> int:
    """Write `src`'s plaintext to `dest`, returning the bytes written.

    This is what crosses the boundary the judge draws: storage is compressed,
    an isolate box is plain.
    """
    with open_plaintext(src) as source, open(dest, "wb") as sink:
        return _copy(source, sink)


def compress_file(path: Path) -> tuple[int, int]:
    """Compress `path` in place, returning (bytes before, bytes after).

    Through a sibling temp file and `os.replace`, so an interrupted migration
    leaves every file either wholly old or wholly new - never a half-written
    test case - and can simply be run again.
    """
    before = path.stat().st_size
    scratch = path.with_name(path.name + ".compressing")
    try:
        with open(path, "rb") as source, gzip.open(
            scratch, "wb", compresslevel=GZIP_LEVEL
        ) as sink:
            _copy(source, sink)
        after = scratch.stat().st_size
        os.replace(scratch, path)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return before, after


def check_storage_format(root: Path) -> None:
    """Refuse a storage root this version cannot read.

    The counterpart of `check_schema_is_current`, and called from the same
    places for the same reason: a server that read a plaintext root would fail
    one request at a time, with the fix buried in a judge log, rather than
    saying so once at startup. Costs an `iterdir` and a `stat`, never a walk -
    a root with the marker is answered by the first line.
    """
    if (root / FORMAT_MARKER).is_file():
        return
    if not root.is_dir():
        return  # `Storage` will create it, and mark it
    if next(root.iterdir(), None) is None:
        return  # empty: nothing to convert, and `Storage` marks it
    raise StorageFormatError(
        f"The storage root {root} holds files but is not marked as compressed. "
        f"Run `{COMPRESS_COMMAND}` to convert it."
    )


class Storage(object):
    """
    Handle storage necessities by abstracting the base directory.

    Every file in it is gzip-compressed, transparently: `write`, `read`,
    `open` and `save_stream` take and return plaintext and are the same calls
    they always were. What is compressed is what sits on the disk in between.
    """

    basedir: Path

    def __init__(self, basedir: Path):
        self.basedir = basedir.resolve()
        if not self.basedir.is_dir():
            self.basedir.mkdir(parents=True)
        marker = self.basedir / FORMAT_MARKER
        # A root with nothing in it is a root with nothing to convert, so a
        # fresh deployment - and every test fixture - is born marked. An
        # existing root is deliberately left unmarked: that is what
        # `check_storage_format` refuses on.
        if not marker.exists() and next(self.basedir.iterdir(), None) is None:
            marker.write_text(FORMAT_GZIP + "\n")

    @contextmanager
    def open(self, path: Path | str, mode="r"):
        target = self.resolve(path)
        # `gzip.open` defaults to binary where the builtin defaults to text, so
        # a bare "r"/"w" here would quietly change what every caller gets back.
        if "b" not in mode and "t" not in mode:
            mode += "t"
        encoding = "utf-8" if "t" in mode else None
        if "r" in mode:
            _require_compressed(target)
        with gzip.open(target, mode, compresslevel=GZIP_LEVEL, encoding=encoding) as fp:
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

    @contextmanager
    def reader(self, path: Path | str):
        """A stored file as a plaintext binary stream.

        For the two places that hand a whole file to somebody else - the sample
        download and the export zip - which must stream it rather than read a
        multi-megabyte input into memory.
        """
        with open_plaintext(self.resolve(path)) as fp:
            yield fp

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
        # Both ends are in storage, so the compressed bytes are copied as they
        # are: there is nothing to decode and re-encode.
        shutil.copy(self.resolve(path_from), self.resolve(path_to))

    def expand_to(self, path: Path | str, dest: Path) -> int:
        """Write a stored file's plaintext to `dest`, outside the store."""
        return expand(self.resolve(path), dest)

    def exists(self, path: Path | str) -> bool:
        return self.resolve(path).is_file()

    def size(self, path: Path | str) -> int:
        """Bytes on disk, which since everything here is compressed is not the
        size of what was put in. `save_stream` returns that."""
        return self.resolve(path).stat().st_size

    def stamp(self, path: Path | str) -> tuple[int, int] | None:
        """A stored file's (bytes on disk, mtime in ns), or None if it is missing.

        What a challenge's version is made of instead of the file's contents
        (see `_challenge_version` in `main.py`): every write here replaces the
        file and so moves its mtime, and a `stat` is what a version can afford
        to ask per test case where a read is not.
        """
        try:
            result = self.resolve(path).stat()
        except FileNotFoundError:
            return None
        return result.st_size, result.st_mtime_ns

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
        """Stream an open file object to `path`, returning the bytes read.

        Test case inputs can be several megabytes (see DESIGN.md), so they are
        compressed straight to disk rather than read into memory. The count is
        of the *plaintext*, so it stays the number the caller handed over -
        what `MAX_TESTCASE_BYTES` is about, and not a compression ratio.

        Deflating tens of megabytes is a second of CPU, so an async caller must
        put this on a thread.
        """
        target = self.resolve(path)
        if not target.parent.is_dir():
            target.parent.mkdir(parents=True)
        with gzip.open(target, "wb", compresslevel=GZIP_LEVEL) as fp:
            return _copy(fileobj, fp)
