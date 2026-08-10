"""Storage keeps user-supplied names inside the storage root.

Once course staff can rename and upload test files, these are the checks that
stop a name from reaching outside the challenge's directory.
"""

from pathlib import Path

import pytest

from cocco.storage import Storage, safe_filename


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
    assert storage.size("challenge_1/in_0.txt") == 4

    storage.rename("challenge_1/in_0.txt", "challenge_1/input.txt")
    assert not storage.exists("challenge_1/in_0.txt")
    assert storage.read("challenge_1/input.txt") == "5 6\n"

    storage.delete("challenge_1/input.txt")
    assert not storage.exists("challenge_1/input.txt")
    # deleting again is not an error: the row may outlive the file
    storage.delete("challenge_1/input.txt")


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
    assert written == 4096
    assert storage.size("challenge_1/big.txt") == 4096
