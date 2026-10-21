"""The transfer API and the CLI for exercise sets: `cocco pull --set` and
`cocco push`, against the real app.

A set is one file, so there is no format under test here - `transfer.py` has
none for it - only the discipline the challenge transfer already has: the
newest version's id as the `ETag`, a conditional push, and a push that is an
import. No judge runs; a build is finished by hand, as `test_exercise_routes.py`
does it.
"""

import re
from contextlib import nullcontext
from pathlib import Path

import pytest
from sqlalchemy import insert

from cocco import cli
from cocco.cli import _read_state
from cocco.model import ApiToken, ExerciseJob, ExerciseJobKind, ExerciseVersionStatus
from cocco.test_exercise_routes import (  # noqa: F401 - `world` is a fixture
    SOURCE,
    TEACHER,
    UNPARSEABLE,
    _build,
    _count,
    _fail,
    _import,
    _imported,
    _versions,
    world,
)
from cocco.tokens import hash_token, mint_token
from cocco.transfer import EXERCISES_NAME


@pytest.fixture
def tokens(world, db):
    minted = {}
    with db.begin() as conn:
        for nickname in ("alice", "bob", "dave", "matteo"):
            minted[nickname] = mint_token()
            conn.execute(insert(ApiToken).values(
                user_id=world[nickname], label="cli",
                token_hash=hash_token(minted[nickname])))
    return minted


@pytest.fixture
def web(make_client, world):
    """Alice in a browser, for the imports the web makes."""
    return make_client(as_=TEACHER)


@pytest.fixture
def api(make_client, world):
    """No session at all: the only identity is the bearer token."""
    return make_client(dummy_auth=False)


@pytest.fixture
def exercise_set(web, world, db) -> int:
    """A set whose first version is built and current."""
    set_id = _imported(web, world)
    _build(db, _versions(db, set_id)[0].id)
    return set_id


def bearer(tokens, nickname: str) -> dict:
    return {"Authorization": f"Bearer {tokens[nickname]}"}


def push(api, tokens, set_id, source, nickname="alice", headers=None):
    body = source.encode() if isinstance(source, str) else source
    return api.post(
        f"/api/exercises/{set_id}",
        headers={**bearer(tokens, nickname), **(headers or {})},
        files={"file": (EXERCISES_NAME, body, "text/markdown")},
    )


EDITED = SOURCE.replace("Print twice the number.", "Print the number, doubled.")


########################################################################
# The routes


async def test_a_pull_is_the_newest_file_verbatim_tagged_with_its_id(
    api, tokens, exercise_set, db
):
    version = _versions(db, exercise_set)[0]
    response = api.get(f"/api/exercises/{exercise_set}", headers=bearer(tokens, "alice"))
    assert response.status_code == 200
    assert response.text == SOURCE
    assert response.headers["etag"] == f'"{version.id}"'
    assert response.headers["x-cocco-version-status"] == "ready"
    assert response.headers["x-cocco-version-number"] == "1"
    assert re.search(
        rf'filename="exercises-{exercise_set}-warm-up\.md"',
        response.headers["content-disposition"],
    )

    again = api.get(
        f"/api/exercises/{exercise_set}",
        headers={**bearer(tokens, "alice"), "If-None-Match": response.headers["etag"]},
    )
    assert again.status_code == 304
    assert again.headers["x-cocco-version-status"] == "ready"


async def test_a_push_is_an_import(api, tokens, exercise_set, db):
    """A new `building` version and its build job; the current version stays
    where it is until that build is ready."""
    before = _versions(db, exercise_set)
    response = push(api, tokens, exercise_set, EDITED,
                    headers={"If-Match": f'"{before[0].id}"'})
    assert response.status_code == 200, response.text

    after = _versions(db, exercise_set)
    assert len(after) == 2
    assert after[1].source == EDITED
    assert after[1].status is ExerciseVersionStatus.building
    assert _count(db, ExerciseJob, ExerciseJob.c.kind == ExerciseJobKind.build,
                  ExerciseJob.c.version_id == after[1].id) == 1

    body = response.json()
    assert body["version"] == str(after[1].id)
    assert (body["number"], body["status"], body["current"]) == (2, "building", False)


async def test_a_push_based_on_an_older_version_is_refused(
    api, web, tokens, world, exercise_set, db
):
    """Somebody re-imported the file on the web since: that is the change a
    stale push would silently replace."""
    stale = _versions(db, exercise_set)[0].id
    assert _import(web, world["course"], EDITED, set_id=exercise_set).status_code == 200

    response = push(api, tokens, exercise_set, SOURCE,
                    headers={"If-Match": f'"{stale}"'})
    assert response.status_code == 412
    assert len(_versions(db, exercise_set)) == 2

    # Without the header - `cocco push --force` - it is applied.
    assert push(api, tokens, exercise_set, SOURCE).status_code == 200
    assert len(_versions(db, exercise_set)) == 3


async def test_a_file_with_errors_is_refused_with_its_problems(
    api, tokens, exercise_set, db
):
    response = push(api, tokens, exercise_set, UNPARSEABLE)
    assert response.status_code == 400
    body = response.json()
    assert "no version was created" in body["detail"]
    assert any(problem["level"] == "error" for problem in body["problems"])
    assert len(_versions(db, exercise_set)) == 1


async def test_a_file_that_names_no_title_keeps_the_sets(api, tokens, exercise_set, db):
    """The upload is always called `exercises.md`, so the file name is no
    title; the set's newest version's is."""
    untitled = SOURCE.replace("---\ntitle: Warm-up\n---\n\n", "")
    assert push(api, tokens, exercise_set, untitled).status_code == 200
    assert _versions(db, exercise_set)[-1].title == "Warm-up"


async def test_a_pull_after_a_failed_reimport_hands_out_the_failed_file(
    api, tokens, web, world, exercise_set, db
):
    """The newest version, not the current one: its author is the person
    about to fix it, and a pull that gave them the old file would take their
    work away."""
    assert _import(web, world["course"], EDITED, set_id=exercise_set).status_code == 200
    failed = _versions(db, exercise_set)[-1]
    _fail(db, failed.id)

    response = api.get(f"/api/exercises/{exercise_set}", headers=bearer(tokens, "alice"))
    assert response.text == EDITED
    assert response.headers["etag"] == f'"{failed.id}"'
    assert response.headers["x-cocco-version-status"] == "failed"


async def test_a_version_reports_its_build(api, tokens, exercise_set, db):
    version_id = push(api, tokens, exercise_set, EDITED).json()["version"]
    url = f"/api/exercises/{exercise_set}/versions/{version_id}"

    building = api.get(url, headers=bearer(tokens, "alice")).json()
    assert (building["status"], building["current"]) == ("building", False)

    _build(db, int(version_id), EDITED)
    ready = api.get(url, headers=bearer(tokens, "alice")).json()
    assert (ready["status"], ready["current"], ready["problems"]) == ("ready", True, [])


async def test_a_version_of_another_set_is_not_found(api, tokens, web, world, db):
    first = _imported(web, world)
    second = _imported(web, world)
    other = _versions(db, second)[0].id
    response = api.get(
        f"/api/exercises/{first}/versions/{other}", headers=bearer(tokens, "alice")
    )
    assert response.status_code == 404


async def test_only_course_staff_reach_a_set(api, tokens, exercise_set):
    """The source carries every solution, so a pull is staff's only even
    though it is a read. A set that does not exist is the same 403."""
    for nickname in ("bob", "dave"):
        assert api.get(
            f"/api/exercises/{exercise_set}", headers=bearer(tokens, nickname)
        ).status_code == 403
        assert push(api, tokens, exercise_set, SOURCE, nickname).status_code == 403
    assert api.get(
        f"/api/exercises/{exercise_set + 1000}", headers=bearer(tokens, "alice")
    ).status_code == 403
    assert api.get(
        f"/api/exercises/{exercise_set}", headers=bearer(tokens, "matteo")
    ).status_code == 200


async def test_no_token_is_a_401(api, exercise_set):
    assert api.get(f"/api/exercises/{exercise_set}").status_code == 401


async def test_the_listing_shows_the_sets_you_can_edit(
    api, tokens, web, world, exercise_set, db
):
    assert _import(web, world["course"], EDITED, set_id=exercise_set).status_code == 200
    _fail(db, _versions(db, exercise_set)[-1].id)

    rows = api.get("/api/exercises", headers=bearer(tokens, "alice")).json()
    assert rows == [{
        "course_id": world["course"], "course_title": "Programming 1",
        "set_id": exercise_set, "title": "Warm-up", "archived": False,
        "latest_number": 2, "latest_status": "failed", "current_number": 1,
    }]
    assert api.get("/api/exercises", headers=bearer(tokens, "bob")).json() == []
    assert api.get("/api/exercises", headers=bearer(tokens, "dave")).json() == []
    admin = api.get("/api/exercises", headers=bearer(tokens, "matteo")).json()
    assert [row["set_id"] for row in admin] == [exercise_set]


########################################################################
# The CLI, against the real app


@pytest.fixture
def run(api, tokens, tmp_path, monkeypatch):
    api.headers.update(bearer(tokens, "alice"))
    monkeypatch.setattr(cli, "_client", lambda args: nullcontext(api))
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "no-config.toml")
    monkeypatch.delenv("COCCO_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    def _run(*argv: str) -> int:
        return cli.main(["--url", "http://testserver", *argv])

    return _run


async def test_a_set_round_trips_through_a_directory(
    run, exercise_set, db, tmp_path, monkeypatch, capsys
):
    assert run("pull", "--set", str(exercise_set)) == 0
    checkout = tmp_path / f"exercises-{exercise_set}-warm-up"
    assert (checkout / EXERCISES_NAME).read_text() == SOURCE
    state = _read_state(checkout)
    assert state is not None
    assert (state.kind, state.id) == (cli.EXERCISE_SET, exercise_set)
    assert state.version == str(_versions(db, exercise_set)[0].id)
    assert set(state.files) == {EXERCISES_NAME}

    monkeypatch.chdir(checkout)
    Path(EXERCISES_NAME).write_text(EDITED)
    assert run("push", "--no-wait") == 0
    versions = _versions(db, exercise_set)
    assert [v.source for v in versions] == [SOURCE, EDITED]
    state = _read_state(checkout)
    assert state is not None and state.version == str(versions[1].id)

    capsys.readouterr()
    assert run("pull") == 0
    out = capsys.readouterr().out
    assert "already up to date" in out
    assert "still being built" in out


async def test_the_cli_refuses_to_push_over_a_web_import_and_then_merges_it(
    run, web, world, exercise_set, db, tmp_path, monkeypatch, capsys
):
    assert run("pull", "--set", str(exercise_set), "-o", "checkout") == 0
    monkeypatch.chdir(tmp_path / "checkout")
    assert _import(web, world["course"], EDITED, set_id=exercise_set).status_code == 200

    Path(EXERCISES_NAME).write_text(SOURCE + "\n")
    assert run("push", "--no-wait") == 1
    assert "cocco pull" in capsys.readouterr().err
    assert len(_versions(db, exercise_set)) == 2

    # One file changed on both sides is a conflict; --force takes the server's
    # and keeps ours under .cocco/discarded/.
    assert run("pull") == 1
    assert run("pull", "--force") == 0
    assert Path(EXERCISES_NAME).read_text() == EDITED


async def test_push_waits_for_the_build_and_a_failure_exits_1(
    run, exercise_set, db, tmp_path, monkeypatch, capsys
):
    assert run("pull", "--set", str(exercise_set), "-o", "checkout") == 0
    monkeypatch.chdir(tmp_path / "checkout")

    # The judge, finishing the build while the CLI waits for it.
    finish = {"with": _build}
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: finish["with"](
        db, _versions(db, exercise_set)[-1].id, EDITED))

    Path(EXERCISES_NAME).write_text(EDITED)
    capsys.readouterr()
    assert run("push") == 0
    assert "now the set's current version" in capsys.readouterr().out

    finish["with"] = lambda db, version_id, source=EDITED: _fail(db, version_id)
    Path(EXERCISES_NAME).write_text(SOURCE)
    assert run("push") == 1
    assert "failed to build" in capsys.readouterr().err


async def test_ls_lists_sets_beside_challenges(run, exercise_set, capsys):
    assert run("ls") == 0
    out = capsys.readouterr().out
    assert "Programming 1" in out
    assert "exercise sets" in out
    # A set whose newest version is current needs no note.
    assert re.search(rf"{exercise_set}\s+Warm-up\n", out)


async def test_an_unchanged_file_is_pushed_again_and_still_conditionally(
    run, exercise_set, db, tmp_path, monkeypatch
):
    """A build can fail for reasons that are not in the file - no judge, a
    judge error - and pushing the same file again is how it is retried. That
    push still sends `If-Match`, so it never needs `--force`."""
    assert run("pull", "--set", str(exercise_set), "-o", "checkout") == 0
    monkeypatch.chdir(tmp_path / "checkout")
    Path(EXERCISES_NAME).write_text(EDITED)
    assert run("push", "--no-wait") == 0
    _fail(db, _versions(db, exercise_set)[-1].id)

    assert run("push", "--no-wait") == 0
    assert [v.source for v in _versions(db, exercise_set)] == [SOURCE, EDITED, EDITED]


async def test_pushes_to_one_set_take_its_lock_in_turn(app_engine, exercise_set):
    """What stops two pushes based on the same version both passing
    `If-Match`: the second waits for the first to commit, and then sees the
    version it made."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from cocco.repository._rest import lock_exercise_set_for_push

    async with app_engine.connect() as first, app_engine.connect() as second:
        assert await lock_exercise_set_for_push(first, exercise_set)
        await second.execute(text("SET lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError):
            await lock_exercise_set_for_push(second, exercise_set)
        await first.rollback()
        await second.rollback()
        assert not await lock_exercise_set_for_push(second, exercise_set + 1000)
