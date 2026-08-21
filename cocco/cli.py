"""`cocco` — the command line half of the transfer API.

Managing a challenge through the web editor is one HTTP form per action: an
upload per test case, an arrow-press per reorder, a `<textarea>` for the
statement. This pulls the whole challenge down as a directory, lets you edit it
with whatever you edit things with, and pushes it back:

    cocco ls
    cocco pull 12                 # -> ./challenge-12-two-sum/
    $EDITOR challenge-12-two-sum/README.md
    cocco push 12 challenge-12-two-sum

Configuration is a URL and a token. Both can be given as flags, but the natural
home is the environment - `COCCO_URL` and `COCCO_TOKEN` - because a token is a
secret, and this repository's rule for secrets is that they live there and have
no spelling in a file. `~/.config/cocco/cli.toml` is the fallback for the URL,
which is not a secret and is tedious to retype.

Note that this is a *client's* configuration and has nothing to do with
`cocco.toml`, which is a deployment's: they name different things and are read
by different processes on different machines.
"""

import argparse
import os
import sys
import tempfile
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

from cocco.transfer import (
    EVALUATOR_DIR,
    MANIFEST_NAME,
    STATEMENT_NAME,
    TESTS_DIR,
)

DEFAULT_URL = "http://localhost:8000"
CONFIG_PATH = Path.home() / ".config" / "cocco" / "cli.toml"

# What a challenge directory is allowed to contain. A push zips these and
# nothing else, so a `.git`, an editor's swap file or a stray note in the
# directory is simply not sent - rather than being sent and refused by the
# server's "this file is not in the manifest" check.
MEMBERS = (MANIFEST_NAME, STATEMENT_NAME)
MEMBER_DIRS = (TESTS_DIR, EVALUATOR_DIR)


class CliError(Exception):
    """Something the user can fix, printed without a traceback."""


def _file_config() -> dict:
    try:
        return tomllib.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CliError(f"{CONFIG_PATH}: {e}") from e


def _client(args) -> httpx.Client:
    """The configured HTTP client, or the sentence explaining what is missing."""
    config = _file_config()
    url = args.url or os.environ.get("COCCO_URL") or config.get("url") or DEFAULT_URL
    token = args.token or os.environ.get("COCCO_TOKEN") or config.get("token")

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
        base_url=url.rstrip("/"), headers=headers, timeout=120.0, follow_redirects=False
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
            sink.write(source.read())


def cmd_pull(args) -> int:
    with _client(args) as client:
        response = client.get(f"/api/challenges/{args.challenge_id}")
        _check(response)
        body = response.content
        disposition = response.headers.get("content-disposition", "")

    # The server names the download after the challenge; fall back to the id,
    # which is the only thing we are certain of.
    suggested = f"challenge-{args.challenge_id}.zip"
    if 'filename="' in disposition:
        suggested = disposition.split('filename="', 1)[1].split('"', 1)[0]

    if args.zip:
        target = Path(args.output or suggested)
        if target.exists() and not args.force:
            raise CliError(f"{target} exists. Pass --force to overwrite it.")
        target.write_bytes(body)
        print(f"{target}")
        return 0

    target = Path(args.output or suggested.removesuffix(".zip"))
    if target.exists() and any(target.iterdir()) and not args.force:
        raise CliError(f"{target}/ is not empty. Pass --force to write into it.")
    target.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryFile() as handle:
        handle.write(body)
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            _extract(archive, target)
            count = sum(
                1 for n in archive.namelist() if n.startswith(f"{TESTS_DIR}/")
            )

    print(f"{target}/  ({count // 2} test case{'' if count == 2 else 's'})")
    print(f"Edit {target}/{STATEMENT_NAME} and {target}/{MANIFEST_NAME}, then:")
    print(f"  cocco push {args.challenge_id} {target}")
    return 0


def _pack(directory: Path, handle) -> None:
    """Zip a challenge directory, and only what belongs in one."""
    if not (directory / MANIFEST_NAME).is_file():
        raise CliError(f"{directory}/{MANIFEST_NAME} is missing - is this a "
                       "challenge directory?")
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in MEMBERS:
            path = directory / name
            if path.is_file():
                archive.write(path, name)
        for subdirectory in MEMBER_DIRS:
            root = directory / subdirectory
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if path.is_file():
                    archive.write(path, f"{subdirectory}/{path.name}")


def cmd_push(args) -> int:
    source = Path(args.path)
    if not source.exists():
        raise CliError(f"{source} does not exist.")

    with tempfile.TemporaryFile() as handle:
        if source.is_dir():
            _pack(source, handle)
            handle.seek(0)
            payload = handle.read()
        else:
            payload = source.read_bytes()

    with _client(args) as client:
        response = client.post(
            f"/api/challenges/{args.challenge_id}",
            files={"file": ("challenge.zip", payload, "application/zip")},
        )
        _check(response)
        summary = response.json()

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

    pull = sub.add_parser("pull", help="download a challenge as a directory")
    pull.add_argument("challenge_id", type=int)
    pull.add_argument("-o", "--output", help="where to write it")
    pull.add_argument("--zip", action="store_true", help="keep it as a zip file")
    pull.add_argument("--force", action="store_true", help="overwrite what is there")
    pull.set_defaults(run=cmd_pull)

    push = sub.add_parser("push", help="upload a challenge directory or zip")
    push.add_argument("challenge_id", type=int)
    push.add_argument("path", nargs="?", default=".")
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
