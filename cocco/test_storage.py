"""Storage keeps user-supplied names inside the storage root, and compresses
what it puts there.

Once course staff can rename and upload test files, these are the checks that
stop a name from reaching outside the challenge's directory. The rest are about
the other half of this module: everything under the root is a gzip stream
stored under its logical name, and every caller goes on handing it plaintext.
"""

import gzip
from pathlib import Path

import pytest

from cocco.storage import (
    FORMAT_MARKER,
    Storage,
    StorageFormatError,
    check_storage_format,
    compress_file,
    expand,
    is_compressed,
    safe_filename,
)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "root")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("in_0.txt", "in_0.txt"),
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        (r"C:\Users\me\input.txt", "input.txt"),
        ("weird name (1).txt", "weird_name__1_.txt"),
        (".hidden", "hidden"),
    ],
)
def test_safe_filename_reduces_to_a_bare_name(name, expected):
    assert safe_filename(name) == expected


@pytest.mark.parametrize("name", ["", "..", ".", "/", "x" * 200])
def test_safe_filename_rejects_unusable_names(name):
    with pytest.raises(ValueError):
        safe_filename(name)


@pytest.mark.parametrize(
    "path", ["../escape.txt", "/etc/passwd", "challenge_1/../../escape.txt"]
)
def test_resolve_refuses_to_leave_the_root(storage: Storage, path):
    with pytest.raises(ValueError):
        storage.resolve(path)


def test_resolve_allows_paths_inside_the_root(storage: Storage):
    resolved = storage.resolve("challenge_1/in_0.txt")
    assert resolved.is_relative_to(storage.basedir)


def test_write_read_rename_delete_round_trip(storage: Storage):
    storage.write("challenge_1/in_0.txt", "5 6\n")
    assert storage.read("challenge_1/in_0.txt") == "5 6\n"
    assert storage.exists("challenge_1/in_0.txt")

    storage.rename("challenge_1/in_0.txt", "challenge_1/input.txt")
    assert not storage.exists("challenge_1/in_0.txt")
    assert storage.read("challenge_1/input.txt") == "5 6\n"

    storage.delete("challenge_1/input.txt")
    assert not storage.exists("challenge_1/input.txt")
    # deleting again is not an error: the row may outlive the file
    storage.delete("challenge_1/input.txt")


def test_delete_directory_removes_an_empty_one(storage: Storage):
    storage.write("challenge_1/in_0.txt", "5 6\n")
    storage.delete("challenge_1/in_0.txt")

    storage.delete_directory("challenge_1")
    assert not storage.resolve("challenge_1").exists()
    # ...and doing it again is not an error, for `delete`'s reason
    storage.delete_directory("challenge_1")


def test_delete_directory_leaves_a_non_empty_one_alone(storage: Storage):
    """`rmdir` and never `rmtree`. Whatever is still in there is something this
    application either did not put there or lost the row for, and a leftover
    file is a thing to look at rather than a thing to lose."""
    storage.write("challenge_1/orphan.txt", "who put this here\n")

    storage.delete_directory("challenge_1")
    assert storage.read("challenge_1/orphan.txt") == "who put this here\n"


@pytest.mark.parametrize("path", ["", ".", "challenge_1/.."])
def test_delete_directory_refuses_the_root(storage: Storage, path):
    """`resolve("")` is the storage root and `resolve` allows it, so this has
    to be refused here rather than left to the traversal check."""
    with pytest.raises(ValueError):
        storage.delete_directory(path)
    assert storage.basedir.is_dir()


def test_rename_refuses_to_clobber(storage: Storage):
    storage.write("a.txt", "a")
    storage.write("b.txt", "b")
    with pytest.raises(ValueError):
        storage.rename("a.txt", "b.txt")
    assert storage.read("b.txt") == "b"


def test_save_stream_writes_without_buffering(storage: Storage, tmp_path: Path):
    source = tmp_path / "big.txt"
    source.write_bytes(b"x" * 4096)
    with open(source, "rb") as fp:
        written = storage.save_stream("challenge_1/big.txt", fp)
    # The count is of what was handed over, not of what landed: it is what
    # `MAX_TESTCASE_BYTES` is about, and a compression ratio would make that
    # limit mean something different for every file.
    assert written == 4096
    with storage.reader("challenge_1/big.txt") as fp:
        assert fp.read() == b"x" * 4096


########################################################################
# Everything under the root is compressed, and nothing outside it is.


def test_what_lands_on_disk_is_compressed(storage: Storage):
    storage.write("challenge_1/in_0.txt", "5 6\n" * 1000)

    on_disk = storage.resolve("challenge_1/in_0.txt")
    assert is_compressed(on_disk)
    assert storage.size("challenge_1/in_0.txt") < 4000
    with gzip.open(on_disk, "rt") as fp:
        assert fp.read() == "5 6\n" * 1000


def test_a_stored_file_is_read_back_as_plaintext(storage: Storage, tmp_path: Path):
    """The three ways out of the store agree, which is what makes compression
    transparent: the text, the stream the downloads use, and the plain file the
    judge expands into a box."""
    storage.write("challenge_1/in_0.txt", "5 6\n")

    assert storage.read("challenge_1/in_0.txt") == "5 6\n"
    with storage.reader("challenge_1/in_0.txt") as fp:
        assert fp.read() == b"5 6\n"
    plain = tmp_path / "expanded.txt"
    assert storage.expand_to("challenge_1/in_0.txt", plain) == 4
    assert plain.read_bytes() == b"5 6\n"


def test_a_plaintext_file_is_refused_by_name(storage: Storage):
    """The whole point of asking up front. `gzip` only notices on the first
    read, which on a million-line input is halfway through grading somebody's
    submission - and what is wrong is the deployment, so the message names the
    command that fixes it rather than the request that tripped over it."""
    storage.resolve("challenge_1").mkdir(parents=True)
    storage.resolve("challenge_1/in_0.txt").write_text("5 6\n")

    with pytest.raises(StorageFormatError, match="compress-storage"):
        storage.read("challenge_1/in_0.txt")
    with pytest.raises(StorageFormatError):
        with storage.reader("challenge_1/in_0.txt"):
            pass


def test_copy_moves_the_stored_bytes(storage: Storage):
    """Both ends are in the store, so there is nothing to decode and re-encode."""
    storage.write("a.txt", "hello\n")
    storage.copy(Path("a.txt"), Path("b.txt"))
    assert storage.read("b.txt") == "hello\n"


def test_compress_file_is_idempotent(tmp_path: Path):
    """What the migration is made of: a run that was interrupted is finished by
    running it again, so it must not compress twice what it compressed once."""
    path = tmp_path / "in_0.txt"
    path.write_bytes(b"5 6\n" * 100)

    before, after = compress_file(path)
    assert before == 400
    assert after == path.stat().st_size
    assert is_compressed(path)

    expanded = tmp_path / "expanded.txt"
    expand(path, expanded)
    assert expanded.read_bytes() == b"5 6\n" * 100
    # ...and nothing left behind by the sibling it was written through.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["expanded.txt", "in_0.txt"]


########################################################################
# The root says which format it is in, the way `alembic_version` says which
# revision a database is at.


def test_a_fresh_root_is_born_marked(tmp_path: Path):
    storage = Storage(tmp_path / "root")
    assert (storage.basedir / FORMAT_MARKER).read_text().strip() == "gzip"
    check_storage_format(storage.basedir)


def test_a_root_that_predates_compression_is_refused(tmp_path: Path):
    root = tmp_path / "root"
    (root / "challenge_1").mkdir(parents=True)
    (root / "challenge_1" / "in_0.txt").write_text("5 6\n")

    # Opening it does not mark it: an existing root is exactly what has to be
    # brought forward, and marking it here would be claiming it already had.
    Storage(root)
    with pytest.raises(StorageFormatError, match="compress-storage"):
        check_storage_format(root)


def test_a_root_that_does_not_exist_yet_is_not_a_refusal(tmp_path: Path):
    """`cocco-web` asks before `Storage` has had a chance to create it."""
    check_storage_format(tmp_path / "nothing-here")


def test_a_stamp_moves_when_a_file_is_rewritten(storage: Storage):
    """What a challenge's version is made of, so a rewrite must show in it."""
    assert storage.stamp("challenge_1/01.in") is None
    storage.write("challenge_1/01.in", "one")
    first = storage.stamp("challenge_1/01.in")
    assert first is not None
    assert storage.stamp("challenge_1/01.in") == first
    storage.write("challenge_1/01.in", "a longer one")
    assert storage.stamp("challenge_1/01.in") != first
