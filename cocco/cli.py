"""`cocco` — the command line half of the transfer API.

Managing a challenge through the web editor is one HTTP form per action: an
upload per test case, an arrow-press per reorder, a `<textarea>` for the
statement. This pulls the whole challenge down as a directory, lets you edit it
with whatever you edit things with, and pushes it back:

    cocco ls
    cocco pull 12                 # -> ./challenge-12-two-sum/
    cd challenge-12-two-sum
    $EDITOR README.md
    cocco push
    cocco pull                    # later: bring in what was changed on the web

**A pulled directory remembers where it came from**, in `.cocco/state.toml`: the
server, the challenge, the version of it the directory last agreed with, and a
fingerprint of every file as it was then. That record is what lets the web
editor and a directory be used side by side:

- a push sends the version as `If-Match`, so pushing over changes somebody made
  on the web since is refused rather than silently undoing them;
- a pull into the directory compares, per file, that record, the directory and
  the server (`reconcile`). A file changed only on the server is taken, a file
  changed only here is kept, and one changed on both sides is a conflict:
  refused, or with `--force` replaced after the local copy is moved into
  `.cocco/discarded/`. Files the server no longer has are removed, which is
  what stops a stale `tests/` file from breaking the next push.

Configuration is a URL and a token. Both can be given as flags, but the natural
home is the environment - `COCCO_URL` and `COCCO_TOKEN` - because a token is a
secret, and this repository's rule for secrets is that they live there and have
no spelling in a file. `~/.config/cocco/cli.toml` is the fallback for the URL,
which is not a secret and is tedious to retype.

The URL in a directory's state is **compared, never used**. The token goes
wherever the URL points, and a directory is something people copy and share, so
a URL read out of one would be a way to collect somebody else's token. A
mismatch is refused, naming the URL to pass.

Note that this is a *client's* configuration and has nothing to do with
`cocco.toml`, which is a deployment's: they name different things and are read
by different processes on different machines.
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import toml

from cocco.transfer import (
    EVALUATOR_DIR,
    MANIFEST_NAME,
    STATEMENT_NAME,
    TESTS_DIR,
    ManifestError,
    dump_manifest,
    parse_manifest,
)

DEFAULT_URL = "http://localhost:8000"
CONFIG_PATH = Path.home() / ".config" / "cocco" / "cli.toml"

# What a challenge directory is allowed to contain. A push zips these and
# nothing else, so a `.git`, an editor's swap file or a stray note in the
# directory is simply not sent - rather than being sent and refused by the
# server's "this file is not in the manifest" check.
MEMBERS = (MANIFEST_NAME, STATEMENT_NAME)
MEMBER_DIRS = (TESTS_DIR, EVALUATOR_DIR)

# Where a pulled directory keeps what it knows about itself. Not a member, so
# it is never pushed; and it ignores itself for git, because the version in it
# is what *this* checkout last agreed with the server on - a colleague who
# clones the repository has agreed with nothing.
STATE_DIR = ".cocco"
STATE_NAME = "state.toml"
INCOMING_DIR = "incoming"
DISCARDED_DIR = "discarded"

_CHUNK = 1 << 20


class CliError(Exception):
    """Something the user can fix, printed without a traceback."""


@dataclass(frozen=True)
class State:
    """`.cocco/state.toml`: what a directory and the server last agreed on.

    `files` maps each member's archive name to its `_fingerprint` as of that
    agreement - the common ancestor `reconcile` compares both sides against.
    `version` is None only against a server too old to send one.
    """

    url: str
    challenge_id: int
    version: str | None
    files: dict[str, str]


@dataclass(frozen=True)
class Reconciliation:
    """What a pull does to each file, decided before anything is written."""

    take: tuple[str, ...]  # the server's version replaces (or removes) ours
    keep: tuple[str, ...]  # changed here only: left alone
    conflicts: tuple[str, ...]  # changed on both sides, differently


def _file_config() -> dict:
    try:
        return tomllib.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CliError(f"{CONFIG_PATH}: {e}") from e


def _resolve_url(args) -> str:
    url = (
        args.url or os.environ.get("COCCO_URL") or _file_config().get("url")
        or DEFAULT_URL
    )
    return url.rstrip("/")


def _client(args) -> httpx.Client:
    """The configured HTTP client, or the sentence explaining what is missing."""
    token = args.token or os.environ.get("COCCO_TOKEN") or _file_config().get("token")

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # No token is not an error *here*: whether one is needed is the server's to
    # say, and a browser session in the cookie jar is another way in. The 401
    # below is where a missing token is explained - including against a
    # development server, which used to sign the CLI in on `[dev] dummy_auth`
    # alone and no longer does: that shortcut is a login box now, and a command
    # line has no browser to type into it.
    return httpx.Client(
        base_url=_resolve_url(args), headers=headers, timeout=120.0,
        follow_redirects=False,
    )


def _check(response: httpx.Response) -> None:
    """Turn a refusal into a sentence. The API answers JSON, so there is one."""
    if response.is_success:
        return
    if response.status_code == 401:
        raise CliError(
            "Not signed in. Mint a token on your profile page in the browser "
            "and set COCCO_TOKEN to it."
        )
    detail = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except ValueError:
        pass
    raise CliError(detail or f"{response.status_code} from the server.")


# ---------------------------------------------------------------------------
# A directory's memory


def _read_state(directory: Path) -> State | None:
    path = directory / STATE_DIR / STATE_NAME
    try:
        table = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CliError(f"{path}: {e}") from e

    url = table.get("url")
    challenge_id = table.get("challenge_id")
    version = table.get("version")
    files = table.get("files", {})
    if (
        not isinstance(url, str)
        or not isinstance(challenge_id, int)
        or isinstance(challenge_id, bool)
        or not (version is None or isinstance(version, str))
        or not isinstance(files, dict)
        or not all(isinstance(value, str) for value in files.values())
    ):
        raise CliError(
            f"{path} is damaged. Delete {directory / STATE_DIR} and pull the "
            "challenge again with --force."
        )
    return State(url=url, challenge_id=challenge_id, version=version, files=files)


def _state_of(directory: Path) -> State | None:
    return _read_state(directory) if directory.is_dir() else None


def _write_state(directory: Path, state: State) -> None:
    root = directory / STATE_DIR
    root.mkdir(exist_ok=True)
    (root / ".gitignore").write_text("*\n")
    document: dict = {"url": state.url, "challenge_id": state.challenge_id}
    if state.version is not None:
        document["version"] = state.version
    document["files"] = dict(sorted(state.files.items()))
    # Through a sibling and a rename, so an interrupted write leaves the old
    # record rather than half of a new one.
    scratch = root / f"{STATE_NAME}.writing"
    scratch.write_text(toml.dumps(document))
    os.replace(scratch, root / STATE_NAME)


def _check_state(
    directory: Path, state: State, url: str, challenge_id: int, *, hint: str = ""
) -> None:
    """Refuse to use a directory against a challenge or server it is not from."""
    if state.challenge_id != challenge_id:
        raise CliError(
            f"{directory}/ was pulled from challenge {state.challenge_id}, not "
            f"{challenge_id}.{hint}"
        )
    if state.url != url:
        raise CliError(
            f"{directory}/ was pulled from {state.url}, but this command is "
            f"talking to {url}. Pass --url {state.url} to use it.{hint}"
        )


def _members(directory: Path) -> dict[str, Path]:
    """Every file a push would send, by its name in the archive."""
    found: dict[str, Path] = {}
    for name in MEMBERS:
        path = directory / name
        if path.is_file():
            found[name] = path
    for subdirectory in MEMBER_DIRS:
        root = directory / subdirectory
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.is_file():
                found[f"{subdirectory}/{path.name}"] = path
    return found


def _fingerprint(name: str, path: Path) -> str:
    """What a file is, for deciding whether it changed.

    The bytes, except for the manifest, which is fingerprinted as the manifest
    it *means*. The server writes `challenge.toml` its own way - header, key
    order, `128.0` for `128` - so a byte comparison would call every
    hand-edited manifest changed on both sides after the next web edit, and a
    pull would overwrite a comment somebody wrote for no change at all. One
    that does not parse is compared as bytes, and so reads as changed.
    """
    if name == MANIFEST_NAME:
        try:
            canonical = dump_manifest(parse_manifest(path.read_text()))
        except (ManifestError, UnicodeDecodeError):
            pass
        else:
            return "manifest:" + hashlib.sha256(canonical.encode()).hexdigest()
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprints(directory: Path) -> dict[str, str]:
    return {name: _fingerprint(name, path) for name, path in _members(directory).items()}


def reconcile(
    base: dict[str, str] | None, local: dict[str, str], incoming: dict[str, str]
) -> Reconciliation:
    """Decide, per file, between what is here and what the server sent.

    `base` is the directory's state: what both sides last agreed on. Absence is
    a value like any other, so a file deleted on one side and untouched on the
    other is simply that side's change. With no base at all - a directory that
    was never pulled - there is nothing to say which side changed a file, so
    every local file that differs from the server's is somebody's work and is a
    conflict.
    """
    take: list[str] = []
    keep: list[str] = []
    conflicts: list[str] = []
    for name in sorted(set(base or {}) | set(local) | set(incoming)):
        mine, theirs = local.get(name), incoming.get(name)
        if mine == theirs:
            continue
        if base is None:
            (take if mine is None else conflicts).append(name)
            continue
        agreed = base.get(name)
        if mine == agreed:
            take.append(name)
        elif theirs == agreed:
            keep.append(name)
        else:
            conflicts.append(name)
    return Reconciliation(tuple(take), tuple(keep), tuple(conflicts))


def _changed_here(directory: Path, state: State) -> list[str]:
    local = _fingerprints(directory)
    return sorted(
        name
        for name in set(local) | set(state.files)
        if local.get(name) != state.files.get(name)
    )


def _unquote(etag: str | None) -> str | None:
    if not etag:
        return None
    return etag.removeprefix("W/").strip('"') or None


def _as_argument(path: Path) -> str:
    """A path as `cocco push` will read it back: all digits would be an id."""
    return f"./{path}" if str(path).isdigit() else str(path)


# ---------------------------------------------------------------------------


def cmd_ls(args) -> int:
    with _client(args) as client:
        response = client.get("/api/challenges")
        _check(response)
        rows = response.json()

    if not rows:
        print("Nothing you can edit. Ask to be enrolled as course staff.")
        return 0

    width = max(len(str(row["challenge_id"])) for row in rows)
    course = None
    for row in rows:
        if row["course_id"] != course:
            course = row["course_id"]
            print(f"\n{row['course_title']}")
        # An archived challenge is still pushable - staff edit one exactly as
        # before - so these are notes beside the title rather than anything
        # missing from the list. The same goes double for an unpublished one:
        # not having published it yet is the commonest reason to be pushing to
        # it, so dropping those would hide the very ids this command exists to
        # make discoverable.
        notes = []
        if row.get("archived"):
            notes.append("archived")
        if not row.get("visible", True):
            notes.append(
                "hidden" if row.get("visible_from") is None
                else f"opens {_when(row['visible_from'])}"
            )
        suffix = f"  ({', '.join(notes)})" if notes else ""
        print(f"  {row['challenge_id']:>{width}}  {row['title']}{suffix}")
    return 0


def _when(iso: str) -> str:
    """A server timestamp as a local wall clock, to the minute.

    The server sends an offset-carrying ISO string and `astimezone()` re-pins it
    to whatever clock this terminal is on - which is the same move
    `render_datetime` makes in the app, one layer down. A value that will not
    parse is printed as it came rather than crashing a listing over a note.
    """
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _extract(archive: zipfile.ZipFile, target: Path) -> None:
    """Unpack a pulled challenge into `target`.

    The members are checked even though this zip came from our own server: a
    client that trusts a path out of a response is a client that can be made to
    write anywhere by whatever the server is next time.
    """
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        destination = (target / name).resolve()
        if not destination.is_relative_to(target.resolve()):
            raise CliError(f"The archive contains an unsafe path: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, open(destination, "wb") as sink:
            shutil.copyfileobj(source, sink, _CHUNK)


def _suggested_name(response: httpx.Response, challenge_id: int) -> str:
    # The server names the download after the challenge; fall back to the id,
    # which is the only thing we are certain of.
    disposition = response.headers.get("content-disposition", "")
    if 'filename="' in disposition:
        return disposition.split('filename="', 1)[1].split('"', 1)[0]
    return f"challenge-{challenge_id}.zip"


def _pull_zip(args) -> int:
    if args.challenge_id is None:
        raise CliError("--zip needs a challenge id: cocco pull <id> --zip")
    with _client(args) as client:
        with client.stream("GET", f"/api/challenges/{args.challenge_id}") as response:
            if not response.is_success:
                response.read()
                _check(response)
            target = Path(args.output or _suggested_name(response, args.challenge_id))
            if target.exists() and not args.force:
                raise CliError(f"{target} exists. Pass --force to overwrite it.")
            with open(target, "wb") as sink:
                for chunk in response.iter_bytes():
                    sink.write(chunk)
    print(f"{target}")
    return 0


def _backup_dir(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target / STATE_DIR / DISCARDED_DIR / stamp
    n = 1
    while candidate.exists():
        n += 1
        candidate = candidate.with_name(f"{stamp}-{n}")
    return candidate


def _apply(
    archive: zipfile.ZipFile, target: Path, state: State | None, *, force: bool
) -> tuple[dict[str, str], Reconciliation, Path | None]:
    """Bring `target` up to a pulled archive, file by file, as `reconcile` says.

    The archive is unpacked into `.cocco/incoming/` first - inside the target,
    so every write after the decision is a rename on one filesystem, and a
    refusal leaves the directory exactly as it was. Returns the server's
    fingerprints (the new base), the decision, and where any discarded local
    copies went.
    """
    staging = target / STATE_DIR / INCOMING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _extract(archive, staging)
        incoming = _fingerprints(staging)
        plan = reconcile(
            None if state is None else state.files, _fingerprints(target), incoming
        )
        if plan.conflicts and not force:
            raise CliError(
                "Changed both here and on the server since the last pull:\n"
                + "".join(f"  {name}\n" for name in plan.conflicts)
                + "Nothing was written. Pass --force to take the server's "
                f"versions - yours are moved into {target / STATE_DIR / DISCARDED_DIR}/ "
                "first - or copy yours somewhere safe and pull again."
            )

        discarded = _backup_dir(target) if plan.conflicts else None
        for name in plan.conflicts:
            mine = target / name
            if discarded is not None and mine.is_file():
                (discarded / name).parent.mkdir(parents=True, exist_ok=True)
                os.replace(mine, discarded / name)

        for name in (*plan.take, *plan.conflicts):
            destination = target / name
            if name in incoming:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging / name, destination)
            else:
                destination.unlink(missing_ok=True)

        # A challenge that lost its evaluator should not leave an empty
        # `evaluator/` behind to suggest otherwise. `rmdir` refuses a directory
        # with anything left in it, which is the point.
        for subdirectory in MEMBER_DIRS:
            try:
                (target / subdirectory).rmdir()
            except OSError:
                pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return incoming, plan, discarded


def cmd_pull(args) -> int:
    if args.zip:
        return _pull_zip(args)

    url = _resolve_url(args)

    # Which directory, when that is knowable before asking the server: `-o`, or
    # the current one when no id was given or it holds the challenge named.
    # `cocco pull 12` typed inside challenge 12's directory refreshes it rather
    # than nesting a second copy inside it.
    target: Path | None = None
    if args.output is not None:
        target = Path(args.output)
    elif args.challenge_id is None:
        target = Path(".")
    else:
        here = _read_state(Path("."))
        if here is not None and here.challenge_id == args.challenge_id:
            target = Path(".")

    state = None if target is None else _state_of(target)
    challenge_id = args.challenge_id
    if challenge_id is None:
        if state is None:
            raise CliError(
                f"{target}/ was not pulled with cocco, so there is nothing to "
                "refresh. Name a challenge: cocco pull <id>"
            )
        challenge_id = state.challenge_id
    if target is not None and state is not None:
        _check_state(target, state, url, challenge_id)

    headers = {}
    if state is not None and state.version is not None:
        headers["If-None-Match"] = f'"{state.version}"'

    with tempfile.TemporaryFile() as handle:
        with _client(args) as client, client.stream(
            "GET", f"/api/challenges/{challenge_id}", headers=headers
        ) as response:
            if response.status_code == 304 and target is not None and state is not None:
                print(f"{target}/  already up to date with challenge {challenge_id}.")
                changed = _changed_here(target, state)
                if changed:
                    print("  changed here and not pushed: " + ", ".join(changed))
                return 0
            if not response.is_success:
                response.read()
                _check(response)
            for chunk in response.iter_bytes():
                handle.write(chunk)
            version = _unquote(response.headers.get("etag"))
            suggested = _suggested_name(response, challenge_id)
        handle.seek(0)

        if target is None:
            target = Path(suggested.removesuffix(".zip"))
            state = _state_of(target)
            if state is not None:
                _check_state(target, state, url, challenge_id)
        if target.exists() and not target.is_dir():
            raise CliError(f"{target} exists and is not a directory.")
        if state is None and target.is_dir() and any(target.iterdir()) and not args.force:
            raise CliError(f"{target}/ is not empty. Pass --force to write into it.")
        target.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(handle) as archive:
            incoming, plan, discarded = _apply(archive, target, state, force=args.force)

    _write_state(
        target,
        State(url=url, challenge_id=challenge_id, version=version, files=incoming),
    )

    count = sum(1 for name in incoming if name.startswith(f"{TESTS_DIR}/")) // 2
    print(f"{target}/  ({count} test case{'' if count == 1 else 's'})")
    if state is None:
        print(f"Edit {target / STATEMENT_NAME} and {target / MANIFEST_NAME}, then:")
        print(f"  cocco push {_as_argument(target)}")
    elif not (plan.take or plan.keep or plan.conflicts):
        print("  already up to date.")
    else:
        if plan.take:
            print("  from the server: " + ", ".join(
                name if name in incoming else f"{name} (removed)"
                for name in plan.take
            ))
        if plan.keep:
            print("  your changes, kept: " + ", ".join(plan.keep))
    if plan.conflicts:
        print("  your changes, replaced by the server's: " + ", ".join(plan.conflicts))
        print(f"  (your versions are in {discarded}/)")
    return 0


def _pack(directory: Path, handle) -> dict[str, str]:
    """Zip a challenge directory, and only what belongs in one.

    Returns the fingerprints of what was packed, which a successful push
    records as the directory's new agreement with the server.
    """
    if not (directory / MANIFEST_NAME).is_file():
        raise CliError(f"{directory}/{MANIFEST_NAME} is missing - is this a "
                       "challenge directory?")
    members = _members(directory)
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in members.items():
            archive.write(path, name)
    return {name: _fingerprint(name, path) for name, path in members.items()}


def _push_arguments(values: list[str]) -> tuple[int | None, str]:
    """`[CHALLENGE_ID] [PATH]`, both optional.

    argparse cannot tell two optional positionals apart, so a lone argument is
    an id exactly when it is all digits; a directory called `12` is `./12`.
    """
    if len(values) > 2:
        raise CliError("push takes at most a challenge id and a path.")
    if len(values) == 2:
        if not values[0].isdigit():
            raise CliError(f"{values[0]!r} is not a challenge id.")
        return int(values[0]), values[1]
    if len(values) == 1:
        if values[0].isdigit():
            return int(values[0]), "."
        return None, values[0]
    return None, "."


def _upload(args, challenge_id: int, payload, headers: dict) -> httpx.Response:
    with _client(args) as client:
        return client.post(
            f"/api/challenges/{challenge_id}",
            headers=headers,
            files={"file": ("challenge.zip", payload, "application/zip")},
        )


def cmd_push(args) -> int:
    given_id, path = _push_arguments(args.arguments)
    source = Path(path)
    if not source.exists():
        raise CliError(f"{source} does not exist.")

    url = _resolve_url(args)
    state = _state_of(source)
    challenge_id = given_id if given_id is not None else (
        None if state is None else state.challenge_id
    )
    if challenge_id is None:
        raise CliError(
            f"Which challenge? {source} was not pulled with cocco, so name it: "
            f"cocco push <id> {_as_argument(source)}"
        )

    # Pushing a directory to a challenge or a server it was not pulled from is
    # how a challenge is copied, so it is allowed - but only when asked for,
    # and it is never conditional, since the version is another challenge's.
    tracked = (
        state is not None and state.challenge_id == challenge_id and state.url == url
    )
    if state is not None and not tracked and not args.force:
        _check_state(
            source, state, url, challenge_id,
            hint=" Pass --force to push it there anyway.",
        )

    headers = {}
    if tracked and state is not None and state.version is not None and not args.force:
        headers["If-Match"] = f'"{state.version}"'

    pushed: dict[str, str] | None = None
    if source.is_dir():
        with tempfile.TemporaryFile() as scratch:
            pushed = _pack(source, scratch)
            scratch.seek(0)
            response = _upload(args, challenge_id, scratch, headers)
    else:
        with open(source, "rb") as payload:
            response = _upload(args, challenge_id, payload, headers)

    if response.status_code == 412:
        refresh = "cocco pull" if source == Path(".") else f"cocco pull -o {source}"
        raise CliError(
            f"Challenge {challenge_id} has changed on the server since {source}/ "
            "was last pulled or pushed - edited on the web, or pushed from "
            f"another copy. Nothing was written. Run `{refresh}` to bring those "
            "changes in (your own edits are kept unless the same file changed on "
            "both sides), then push again - or pass --force to overwrite them."
        )
    _check(response)
    summary = response.json()

    # A directory that was never pulled is adopted by a successful push: it is
    # now exactly what the server has. One forced onto another challenge keeps
    # describing the challenge it came from.
    if pushed is not None and (state is None or tracked):
        _write_state(
            source,
            State(url=url, challenge_id=challenge_id,
                  version=summary.get("version"), files=pushed),
        )

    testcases = summary["testcases"]
    print(f"{summary['title']} (challenge {summary['challenge_id']})")
    print(f"  test cases: {testcases['updated']} updated, {testcases['added']} added")
    if testcases["retired"]:
        print(
            "  retired (they have graded submissions): "
            + ", ".join(testcases["retired"])
        )
    if testcases["deleted"]:
        print("  deleted: " + ", ".join(testcases["deleted"]))
    print(f"  evaluator: {summary['evaluator'] or 'the bundled default'}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cocco", description=(__doc__ or "").split("\n\n")[0]
    )
    parser.add_argument("--url", help=f"the cocco server (default {DEFAULT_URL})")
    parser.add_argument("--token", help="an API token; prefer COCCO_TOKEN")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("ls", help="the challenges you can edit")
    listing.set_defaults(run=cmd_ls)

    pull = sub.add_parser(
        "pull", help="download a challenge as a directory, or refresh one"
    )
    pull.add_argument(
        "challenge_id", type=int, nargs="?",
        help="default: the challenge the directory was pulled from",
    )
    pull.add_argument(
        "-o", "--output",
        help="where to write it (default: this directory if it holds that "
             "challenge, otherwise a new one named after it)",
    )
    pull.add_argument("--zip", action="store_true", help="keep it as a zip file")
    pull.add_argument(
        "--force", action="store_true",
        help="write into a directory cocco did not pull, or take the server's "
             "side of a conflict (your copies go to .cocco/discarded/)",
    )
    pull.set_defaults(run=cmd_pull)

    push = sub.add_parser(
        "push", help="upload a challenge directory or zip",
        usage="cocco push [-h] [--force] [CHALLENGE_ID] [PATH]",
    )
    push.add_argument(
        "arguments", nargs="*", metavar="ARG",
        help="[CHALLENGE_ID] [PATH]: the id defaults to the one the directory "
             "was pulled from, the path to the current directory",
    )
    push.add_argument(
        "--force", action="store_true",
        help="push even if the challenge changed on the server since the last "
             "pull, or to a challenge the directory was not pulled from",
    )
    push.set_defaults(run=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except CliError as e:
        print(f"cocco: {e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"cocco: cannot reach the server: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
