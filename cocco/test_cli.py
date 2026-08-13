"""The CLI's own logic: what it packs, what it unpacks, what it says.

The HTTP half is `test_api_routes.py`'s business. What is left here is the part
that touches the user's filesystem, which is the part that can lose their work.
"""

import zipfile

import httpx
import pytest

from cocco.cli import CliError, _extract, _pack, _check, build_parser
from cocco.transfer import MANIFEST_NAME, STATEMENT_NAME


@pytest.fixture
def challenge_dir(tmp_path):
    directory = tmp_path / "challenge-12-two-sum"
    (directory / "tests").mkdir(parents=True)
    (directory / "evaluator").mkdir()
    (directory / MANIFEST_NAME).write_text('title = "Two Sum"\n')
    (directory / STATEMENT_NAME).write_text("Find them.\n")
    (directory / "tests" / "01.in").write_text("1 2\n")
    (directory / "tests" / "01.out").write_text("3\n")
    (directory / "evaluator" / "checker.py").write_text("print()\n")
    return directory


def _packed(directory, tmp_path) -> zipfile.ZipFile:
    target = tmp_path / "out.zip"
    with open(target, "wb") as handle:
        _pack(directory, handle)
    return zipfile.ZipFile(target)


def test_a_directory_packs_to_exactly_the_transfer_format(challenge_dir, tmp_path):
    with _packed(challenge_dir, tmp_path) as archive:
        assert sorted(archive.namelist()) == sorted([
            MANIFEST_NAME, STATEMENT_NAME,
            "tests/01.in", "tests/01.out", "evaluator/checker.py",
        ])
        assert archive.read("tests/01.in").decode() == "1 2\n"


def test_nothing_else_in_the_directory_is_sent(challenge_dir, tmp_path):
    """A `.git`, an editor's swap file or a stray note would otherwise be
    uploaded and then refused by the server's "not in the manifest" check -
    which would be a confusing way to learn that vim was running."""
    (challenge_dir / "notes.txt").write_text("todo")
    (challenge_dir / ".git").mkdir()
    (challenge_dir / ".git" / "HEAD").write_text("ref: x")
    (challenge_dir / "tests" / ".01.in.swp").write_bytes(b"\0")

    with _packed(challenge_dir, tmp_path) as archive:
        names = archive.namelist()
    assert "notes.txt" not in names
    assert not any(name.startswith(".git") for name in names)
    # ...but a dotfile *inside* tests/ is still a file in tests/, and the
    # server is the one that decides whether the manifest names it.
    assert "tests/.01.in.swp" in names


def test_a_directory_that_is_not_a_challenge_is_refused(tmp_path):
    (tmp_path / "tests").mkdir()
    with pytest.raises(CliError, match=MANIFEST_NAME):
        with open(tmp_path / "out.zip", "wb") as handle:
            _pack(tmp_path, handle)


def test_extracting_refuses_a_path_that_escapes_the_target(tmp_path):
    """This zip came from our own server, which is exactly the assumption not
    to build a client on: a client that trusts a path out of a response can be
    made to write anywhere by whatever the server is next time."""
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escaped.txt", "x")

    target = tmp_path / "into"
    target.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(CliError, match="unsafe path"):
            _extract(archive, target)
    assert not (tmp_path / "escaped.txt").exists()


def test_a_round_trip_through_pack_and_extract_is_the_identity(
    challenge_dir, tmp_path
):
    target = tmp_path / "back"
    target.mkdir()
    with _packed(challenge_dir, tmp_path) as archive:
        _extract(archive, target)
    assert (target / "tests" / "01.out").read_text() == "3\n"
    assert (target / MANIFEST_NAME).read_text() == 'title = "Two Sum"\n'


def test_a_401_says_what_to_do_about_it():
    """The failure every first-time user hits, so it names the fix rather than
    the status code."""
    response = httpx.Response(401, json={"detail": "No API token."})
    with pytest.raises(CliError, match="COCCO_TOKEN"):
        _check(response)


def test_a_refusal_is_reported_as_the_server_worded_it():
    response = httpx.Response(400, json={"detail": "tests/03.in is not named."})
    with pytest.raises(CliError, match="tests/03.in"):
        _check(response)


def test_a_refusal_without_a_json_body_still_raises():
    with pytest.raises(CliError, match="500"):
        _check(httpx.Response(500, text="<html>oops</html>"))


def test_a_success_passes_through():
    assert _check(httpx.Response(200, json=[])) is None


def test_push_defaults_to_the_current_directory():
    args = build_parser().parse_args(["push", "12"])
    assert args.path == "."


def test_pull_extracts_unless_told_otherwise():
    assert build_parser().parse_args(["pull", "12"]).zip is False
    assert build_parser().parse_args(["pull", "12", "--zip"]).zip is True
