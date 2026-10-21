"""Module-level state every web module shares, and the seams tests patch."""

import re
import zipfile
from typing import Annotated

from fastapi import Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from cocco.config import config_path, load_config
from cocco.judge import extension_to_language, load_languages
from cocco.model import UserRole
from cocco.storage import Storage

# Everything the app is configured with: the database, the storage root, the
# allowed domains, the session cookie and the development shortcuts. A missing
# file is fine - the app boots on the defaults, and the login page says that
# no sign-in method is configured.
CONFIG = load_config(config_path())

# Development shortcut: no provider and no credentials. `/login` grows a box, and
# whatever address is typed into it is signed in - so it is `post_auth_dummy`
# that branches on this, and `get_user` no longer does. On anything public it is
# equivalent to publishing every account's password.
#
# Derived from the config once, here, and thereafter *this name* is the app's
# switch, read rather than `CONFIG.dev`: it is the seam the route tests flip,
# and a frozen dataclass field has no equivalent. Do not "tidy" it away.
DUMMY_AUTH = CONFIG.dev.dummy_auth

# The database. The judge builds a *sync* engine from this same URL, which is
# why the driver in it has to be one with both faces - see DEFAULT_DATABASE_URL.
engine = create_async_engine(CONFIG.database.url)

# Test case inputs and expected outputs live on the filesystem, not in the
# database (see DESIGN.md). The judge resolves the same paths against the same
# root, and the two agree because they read it out of the same file.
storage = Storage(CONFIG.storage.root)

# How a stored file is handed to somebody outside. Everything under the storage
# root is compressed and nothing the other end reads is, so a download is a
# decompressing stream rather than a `FileResponse`: there is no file holding
# the right bytes to point one at, and a million-line test case must not be
# inflated into memory to make one. The generator is deliberately *sync* -
# Starlette iterates one of those on a thread, which is what keeps the inflate
# off the event loop.
def _stored_file_download(relative: str, filename: str) -> StreamingResponse:
    def chunks():
        with storage.reader(relative) as fp:
            while chunk := fp.read(1 << 16):
                yield chunk

    return StreamingResponse(
        chunks(),
        # text/plain whatever the name says, so a file named .html can never be
        # served as a page on this origin.
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _zip_stored(zf: zipfile.ZipFile, relative: str, arcname: str) -> None:
    """Copy a stored file into an open zip, decompressing on the way.

    `zf.write` would put the gzip bytes in under a plain name: what leaves here
    is the transfer format, which is plaintext at both ends and is a directory
    somebody edits. Through `zf.open(..., "w")` rather than `writestr`, so a
    million-line test case is still never held in memory.
    """
    with storage.reader(relative) as source, zf.open(arcname, "w") as sink:
        while chunk := source.read(1 << 20):
            sink.write(chunk)


# Uploads are mapped to a language by their extension, so the challenge page
# needs no language picker. `languages.toml` is the same file the judge loads -
# literally, since `load_languages` resolves it against the checkout rather
# than the working directory - and so is the mapping: an evaluator script is
# recognised by exactly the rule a submitted solution is.
LANGUAGES = load_languages()
EXTENSION_TO_LANGUAGE = extension_to_language(LANGUAGES)

MAX_UPLOAD_BYTES = 1 << 20

# Course staff. `role` lives on `enrollment`, so these are per-course, not
# site-wide. Everyone else - i.e. students and the unenrolled - is read-only.
# A site administrator is staff on every course without appearing here: that is
# resolved in `effective_course_role`, not by widening this set.
EDITOR_ROLES = {UserRole.teacher, UserRole.collaborator}
# The same set as it comes back from `row_to_json`, i.e. plain strings
EDITOR_ROLE_NAMES = {role.value for role in EDITOR_ROLES}


async def get_db():
    async with engine.connect() as conn:
        yield conn

DBDep = Annotated[AsyncConnection, Depends(get_db)]


def _slug(text: str, fallback: str = "challenge") -> str:
    """A file-name-safe stub of a title, for the download's name only.

    Nothing is ever looked up by it: the challenge a push lands on is the one
    in the URL. It exists so a directory full of pulled challenges can be read.
    """
    stub = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return stub[:60] or fallback
