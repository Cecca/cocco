"""The CLI's own logic: what it packs, what it unpacks, what it says.

The HTTP half is `test_api_routes.py`'s business. What is left here is the part
that touches the user's filesystem, which is the part that can lose their work.
"""

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from cocco import cli
from cocco.cli import (
    STATE_DIR,
    CliError,
    _check,
    _extract,
    _pack,
    _push_arguments,
    _read_state,
    build_parser,
    reconcile,
)
from cocco.model import LeaderboardRanking
from cocco.transfer import (
    MANIFEST_NAME,
    STATEMENT_NAME,
    Manifest,
    ManifestTestCase,
    dump_manifest,
)


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


@pytest.mark.parametrize(
    "argv, expected",
    [
        ([], (None, ".")),
        (["12"], (12, ".")),
        (["checkout"], (None, "checkout")),
        (["12", "checkout"], (12, "checkout")),
        (["./12"], (None, "./12")),
    ],
)
def test_push_takes_an_optional_id_and_an_optional_path(argv, expected):
    assert _push_arguments(argv) == expected


def test_push_refuses_a_path_where_the_id_goes():
    with pytest.raises(CliError, match="not a challenge id"):
        _push_arguments(["checkout", "12"])


def test_pull_extracts_unless_told_otherwise():
    assert build_parser().parse_args(["pull", "12"]).zip is False
    assert build_parser().parse_args(["pull", "12", "--zip"]).zip is True


# ── deciding a pull ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base, local, incoming, expected",
    [
        ({"a": "1"}, {"a": "1"}, {"a": "2"}, ("take", "a")),
        ({"a": "1"}, {"a": "2"}, {"a": "1"}, ("keep", "a")),
        ({"a": "1"}, {"a": "2"}, {"a": "2"}, None),
        ({"a": "1"}, {"a": "2"}, {"a": "3"}, ("conflicts", "a")),
        ({"a": "1"}, {"a": "1"}, {}, ("take", "a")),
        ({"a": "1"}, {"a": "2"}, {}, ("conflicts", "a")),
        ({"a": "1"}, {}, {"a": "1"}, ("keep", "a")),
        ({}, {"a": "1"}, {}, ("keep", "a")),
        (None, {"a": "1"}, {}, ("conflicts", "a")),
        (None, {}, {"a": "1"}, ("take", "a")),
    ],
    ids=[
        "changed-on-the-server", "changed-here", "same-change-on-both",
        "different-changes", "removed-on-the-server",
        "removed-there-edited-here", "removed-here", "added-here",
        "never-pulled-local-file", "never-pulled-server-file",
    ],
)
def test_reconcile(base, local, incoming, expected):
    plan = reconcile(base, local, incoming)
    decided = {"take": plan.take, "keep": plan.keep, "conflicts": plan.conflicts}
    wanted = {"take": (), "keep": (), "conflicts": ()}
    if expected is not None:
        kind, name = expected
        wanted[kind] = (name,)
    assert decided == wanted


# ── pull and push against a stand-in server ─────────────────────────────────

URL = "http://cocco.test"
CHECKOUT = "challenge-12-two-sum"


def manifest(*names: str, header: str = "# the server's header\n") -> str:
    return dump_manifest(
        Manifest(
            title="Two Sum", ranking=LeaderboardRanking.total_score,
            max_time_s=1.0, max_memory_mb=64.0, max_threads=1, evaluator=None,
            testcases=tuple(
                ManifestTestCase(input=f"tests/{n}.in", expected_output=f"tests/{n}.out")
                for n in names
            ),
        ),
        header,
    )


class FakeServer:
    """Challenge 12 as the transfer API serves it: a zip, a version that moves
    on every change, and `If-None-Match` / `If-Match` honoured.

    The HTTP half is tested against the real app in `test_api_routes.py`; what
    matters here is what the CLI does to a directory in between.
    """

    def __init__(self):
        self.files: dict[str, str] = {
            MANIFEST_NAME: manifest("01"),
            STATEMENT_NAME: "Find them.\n",
            "tests/01.in": "1 2\n",
            "tests/01.out": "3\n",
        }
        self.version = 1
        self.statuses: list[int] = []
        self.pushes = 0
        self.if_match: str | None = None

    @property
    def etag(self) -> str:
        return f'"v{self.version}"'

    def edit(self, changes: dict[str, str | None]) -> None:
        """A web edit: set or remove files, and move the version."""
        for name, contents in changes.items():
            if contents is None:
                self.files.pop(name)
            else:
                self.files[name] = contents
        self.version += 1

    def export(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/challenges/12"
        if request.headers.get("if-none-match") == self.etag:
            response = httpx.Response(304, headers={"etag": self.etag})
        else:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, contents in self.files.items():
                    archive.writestr(name, contents)
            response = httpx.Response(200, content=buffer.getvalue(), headers={
                "etag": self.etag,
                "content-disposition": f'attachment; filename="{CHECKOUT}.zip"',
            })
        self.statuses.append(response.status_code)
        return response

    def upload(self, args, kind, challenge_id, payload, headers) -> httpx.Response:
        self.pushes += 1
        self.if_match = headers.get("If-Match")
        if self.if_match is not None and self.if_match != self.etag:
            return httpx.Response(412, json={"detail": "changed"})
        with zipfile.ZipFile(payload) as archive:
            self.files = {n: archive.read(n).decode() for n in archive.namelist()}
        self.version += 1
        return httpx.Response(200, json={
            "challenge_id": challenge_id, "title": "Two Sum",
            "version": f"v{self.version}", "evaluator": None,
            "testcases": {"updated": 1, "added": 0, "retired": [], "deleted": []},
        })


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(cli, "_client", lambda args: httpx.Client(
        base_url=URL, transport=httpx.MockTransport(fake.export)))
    monkeypatch.setattr(cli, "_upload", fake.upload)
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "no-config.toml")
    monkeypatch.delenv("COCCO_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    return fake


def run(*argv: str) -> int:
    return cli.main(["--url", URL, *argv])


@pytest.fixture
def checkout(server, tmp_path, monkeypatch) -> Path:
    """Challenge 12 pulled, and the current directory."""
    assert run("pull", "12") == 0
    monkeypatch.chdir(tmp_path / CHECKOUT)
    return tmp_path / CHECKOUT


def test_a_pull_records_where_the_directory_came_from(server, checkout):
    state = _read_state(checkout)
    assert state is not None
    assert (state.url, state.id, state.version) == (URL, 12, "v1")
    assert set(state.files) == set(server.files)
    assert (checkout / STATE_DIR / ".gitignore").read_text() == "*\n"


def test_a_push_needs_no_arguments_and_is_conditional_on_the_pull(server, checkout):
    Path("tests/01.out").write_text("4\n")
    assert run("push") == 0
    assert server.if_match == '"v1"'
    assert server.files["tests/01.out"] == "4\n"
    # ...and the directory now agrees with what that push made.
    state = _read_state(checkout)
    assert state is not None and state.version == "v2"
    assert run("pull") == 0
    assert server.statuses[-1] == 304


def test_a_push_over_a_web_edit_is_refused(server, checkout, capsys):
    server.edit({STATEMENT_NAME: "Find them fast.\n"})
    Path("tests/01.out").write_text("4\n")
    assert run("push") == 1
    assert "cocco pull" in capsys.readouterr().err
    assert server.files[STATEMENT_NAME] == "Find them fast.\n"
    assert server.files["tests/01.out"] == "3\n"


def test_force_pushes_over_a_web_edit(server, checkout):
    server.edit({STATEMENT_NAME: "Find them fast.\n"})
    assert run("push", "--force") == 0
    assert server.if_match is None
    assert server.files[STATEMENT_NAME] == "Find them.\n"


def test_a_pull_takes_the_servers_changes_and_keeps_yours(server, checkout):
    Path(STATEMENT_NAME).write_text("Mine.\n")
    server.edit({
        MANIFEST_NAME: manifest("01", "02"),
        "tests/01.out": "three\n", "tests/02.in": "2 2\n", "tests/02.out": "4\n",
    })

    assert run("pull") == 0
    assert Path(STATEMENT_NAME).read_text() == "Mine.\n"
    assert Path("tests/01.out").read_text() == "three\n"
    assert Path("tests/02.in").read_text() == "2 2\n"

    # The push a web edit refused now goes through, carrying both sides.
    assert run("push") == 0
    assert server.if_match == '"v2"'
    assert server.files[STATEMENT_NAME] == "Mine.\n"
    assert server.files["tests/02.in"] == "2 2\n"


def test_a_test_case_removed_on_the_server_leaves_no_file_behind(server, checkout):
    """What `pull --force` into an old directory used to leave: a `tests/` file
    the manifest no longer names, which the next push was refused over."""
    server.edit({
        MANIFEST_NAME: manifest("01", "02"), "tests/02.in": "x\n", "tests/02.out": "y\n",
    })
    assert run("pull") == 0
    server.edit({MANIFEST_NAME: manifest("01"), "tests/02.in": None, "tests/02.out": None})
    assert run("pull") == 0
    assert sorted(p.name for p in Path("tests").iterdir()) == ["01.in", "01.out"]


def test_a_file_changed_on_both_sides_is_refused_and_nothing_is_written(
    server, checkout, capsys
):
    Path("tests/01.out").write_text("mine\n")
    server.edit({"tests/01.out": "theirs\n", STATEMENT_NAME: "New.\n"})

    assert run("pull") == 1
    assert "tests/01.out" in capsys.readouterr().err
    assert Path("tests/01.out").read_text() == "mine\n"
    assert Path(STATEMENT_NAME).read_text() == "Find them.\n"
    state = _read_state(checkout)
    assert state is not None and state.version == "v1"


def test_force_takes_the_servers_side_and_keeps_a_copy_of_yours(server, checkout):
    Path("tests/01.out").write_text("mine\n")
    server.edit({"tests/01.out": "theirs\n"})

    assert run("pull", "--force") == 0
    assert Path("tests/01.out").read_text() == "theirs\n"
    [kept] = (checkout / STATE_DIR / "discarded").iterdir()
    assert (kept / "tests" / "01.out").read_text() == "mine\n"


def test_an_unchanged_challenge_is_not_downloaded_again(server, checkout, capsys):
    Path(STATEMENT_NAME).write_text("Mine.\n")
    capsys.readouterr()
    assert run("pull") == 0
    assert server.statuses == [200, 304]
    assert STATEMENT_NAME in capsys.readouterr().out
    assert Path(STATEMENT_NAME).read_text() == "Mine.\n"


def test_pulling_the_same_id_inside_its_directory_refreshes_it(server, checkout):
    assert run("pull", "12") == 0
    assert not Path(CHECKOUT).exists()


def test_a_directory_is_never_used_against_another_server(server, checkout, capsys):
    """The token goes wherever the URL points, so a URL is never taken out of a
    directory somebody may have handed over - a mismatch is refused instead."""
    assert cli.main(["--url", "http://elsewhere.test", "push"]) == 1
    assert f"--url {URL}" in capsys.readouterr().err
    assert server.pushes == 0


def test_rewriting_the_manifest_by_hand_is_not_a_change(server, checkout):
    """The server writes `challenge.toml` its own way, so a byte comparison would
    make every hand-edited manifest a conflict at the next web edit."""
    hand_written = "# my notes\n" + Path(MANIFEST_NAME).read_text().replace(
        "max_threads = 1", "max_threads   =   1"
    )
    Path(MANIFEST_NAME).write_text(hand_written)
    server.edit({
        MANIFEST_NAME: manifest("01", header="# a reworded header\n"),
        STATEMENT_NAME: "New.\n",
    })

    assert run("pull") == 0
    assert Path(MANIFEST_NAME).read_text() == hand_written
    assert Path(STATEMENT_NAME).read_text() == "New.\n"


def test_a_directory_that_was_never_pulled_is_adopted_by_a_push(server, tmp_path):
    directory = tmp_path / "by-hand"
    (directory / "tests").mkdir(parents=True)
    for name, contents in server.files.items():
        (directory / name).write_text(contents)

    assert run("push", "12", "by-hand") == 0
    assert server.if_match is None
    state = _read_state(directory)
    assert state is not None and (state.id, state.version) == (12, "v2")


def test_ls_still_lists_challenges_on_a_server_without_exercise_sets(
    tmp_path, monkeypatch, capsys
):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/challenges":
            return httpx.Response(200, json=[{
                "course_id": 1, "course_title": "Algorithms", "challenge_id": 12,
                "title": "Two Sum", "archived": False, "visible": True,
                "visible_from": "2026-01-01T00:00:00+00:00",
            }])
        return httpx.Response(404, json={"detail": "Not Found"})

    monkeypatch.setattr(cli, "_client", lambda args: httpx.Client(
        base_url=URL, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "no-config.toml")
    assert run("ls") == 0
    out = capsys.readouterr().out
    assert "12  Two Sum" in out and "exercise sets" not in out
