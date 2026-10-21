"""The Jinja environment: directories, static files, filters and globals."""

from urllib.parse import parse_qs

import markdown as md
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound
from pathlib import Path

from cocco.exercises.hints import error_hint
from cocco.model import UserRole
from cocco.web.core import EDITOR_ROLE_NAMES

# Static files. Resolved against the package rather than the working directory,
# the rule `load_languages` already follows for `languages.toml` - and inside
# `cocco/` rather than beside it, so an installed `cocco-web` has them too.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# The templates, by the same rule. Both of these used to be bare relative
# strings handed to Starlette and Jinja, which meant the app ran from the
# repository root and nowhere else - and, worse for `static`, meant the mount
# and `static_url` below could name two different directories.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class VersionedStaticFiles(StaticFiles):
    """`StaticFiles` with a `Cache-Control`, chosen by whether the URL carries a
    `?v=`.

    Starlette sets a `last-modified` and an `etag` and no freshness directive at
    all, so a browser falls back to heuristic caching - which for a file that
    was created recently is worth nothing. Every reload therefore revalidated
    the navbar mark, and the navbar painted an empty box until the 304 came
    back. That is the flicker.

    A year of `immutable` is only safe on a URL that changes when the file does,
    which is what `static_url` stamps on; a URL typed by hand has no version to
    change and so gets `no-cache` - it may be held in cache, but never used
    without asking. The two halves have to stay together: the header is the
    point of the version, and the version is what makes the header safe."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # The `v` *key*, not the substring "v=", which `?nv=1` also contains.
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        # Set after `super()`, so this covers the 304 too: `NotModifiedResponse`
        # keeps `cache-control` among the few headers it carries over.
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "no-cache"
        )
        return response

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# `codehilite` is what puts a fenced block through Pygments; `fenced_code` hands
# every fence to it once it is registered, and it picks up indented blocks itself,
# so a statement has one code-block shape and not two. The configuration is three
# deliberate choices:
#   css_class - `cocco-code`, the scope `assets/styles.css` writes the token
#     colours under. The default is `codehilite`, which matches nothing here.
#   guess_lang - off. A fence with no language stays plain: guessing is slow, and
#     on the three lines of *sample input* a statement usually fences it would
#     colour data as if it were code.
#   linenums - off. Line numbers belong on a submission, where a compiler error
#     names one. They are also what makes `render_source`'s output a
#     `.cocco-codetable`; without them a fence is the plain `<div><pre><code>` the
#     `.cocco-code pre` rule already fits.
# An unknown fence language cannot raise: `CodeHilite.hilite` falls back to the
# `text` lexer, which is the same `TextLexer` fallback `render_source` spells by
# hand, so ```pseudocode renders plain rather than 500ing a challenge page.
_MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "codehilite"]
_MARKDOWN_EXTENSION_CONFIGS = {
    "codehilite": {"css_class": "cocco-code", "guess_lang": False, "linenums": False},
}


def render_markdown(text: str | None) -> Markup:
    html = md.markdown(
        text or "",
        extensions=_MARKDOWN_EXTENSIONS,
        extension_configs=_MARKDOWN_EXTENSION_CONFIGS,
    )
    return Markup(html)

# Line numbers in their own table column so that selecting the code does not
# select them too (see `.linenos` in assets/styles.css). `cocco-code` is the
# class the token styles are scoped under.
_SOURCE_FORMATTER = HtmlFormatter(cssclass="cocco-code", linenos="table", wrapcode=True)


def render_source(source: str, language: str) -> Markup:
    """A submission's source, highlighted. `language` is the name from
    `languages.toml`, which for every language we support happens to be a
    Pygments alias too; anything else falls back to unhighlighted text."""
    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        lexer = TextLexer()
    return Markup(pygments_highlight(source, lexer, _SOURCE_FORMATTER))


def render_datetime(value):
    """A timestamp as a sentence, in this server's own zone.

    Two shapes arrive here, because the page queries answer in JSON: a real
    `datetime` from a plain Core read, and an ISO-8601 *string* from `json_agg`
    / `row_to_json`. Both are now offset-carrying, in whatever zone the Postgres
    session happens to be set to - which is the host's for the devShell's
    cluster and UTC for the container in `compose.yaml`. `astimezone()` with no
    argument pins the reading to the app server's local clock, so the two
    databases print the same sentence, and it is the same sentence the naive
    `datetime.now()` this schema used to store already produced.
    """
    from babel.dates import format_datetime
    from datetime import datetime
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone()
    return format_datetime(value, locale="en")

def render_datetime_local(value) -> str:
    """A timestamp as an `<input type="datetime-local">` value.

    `render_datetime`'s twin, and it has to be: that filter prints the sentence
    a badge shows and this one fills the box a teacher types into, so if the two
    disagreed about which clock they were on, a challenge would open an hour
    away from where it said it would. Hence the same two shapes accepted - a
    real `datetime` from a plain Core read, an ISO-8601 string from `row_to_json`
    - and the same `astimezone()` with no argument, pinning it to the app
    server's local clock.

    The seconds are dropped because the input's default `step` has no place for
    them, and an empty string for NULL, which is an empty box.
    """
    from datetime import datetime
    if value is None:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y-%m-%dT%H:%M")

def render_score(value) -> str:
    """Per-test scores are in [0, 1]; the UI shows their mean out of 100."""
    if value is None:
        return "—"
    return f"{round(float(value) * 100)}"

def render_duration(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value < 1:
        return f"{value * 1000:.0f} ms"
    return f"{value:.2f} s"

templates.env.filters["markdown"] = render_markdown
templates.env.filters["highlight"] = render_source
templates.env.filters["datetime"] = render_datetime
templates.env.filters["datetime_local"] = render_datetime_local
templates.env.filters["score"] = render_score
templates.env.filters["duration"] = render_duration

# A global rather than a context key: `_course-card.jinja` is included from two
# places and only needs to know whether a `role` string means course staff.
templates.env.globals["EDITOR_ROLE_NAMES"] = EDITOR_ROLE_NAMES

# Every role there is, in the order the people page offers them - strings,
# because that is what `row_to_json` gives the template to compare against.
# A global for the same reason: the fragment that builds the selector is served
# from four routes, and the list of roles is not any of their business.
USER_ROLE_NAMES = [role.value for role in UserRole]
templates.env.globals["USER_ROLE_NAMES"] = USER_ROLE_NAMES


def static_url(path: str) -> str:
    """`/static/<path>` with the file's mtime as `?v=`, which is what opts it
    into `VersionedStaticFiles`' year of `immutable`.

    The `stat` happens per render rather than once at import, and that is
    load-bearing: `npm run watch:css` rewrites `cocco/static/styles.css` without
    touching a `.py`, so a version fixed at import would pin a year-long
    `max-age` to a file that has since changed, with nothing left to invalidate
    it. Three files and one `stat` each is not worth caching around.

    A missing file falls back to the bare path - `cocco/static/styles.css` is
    a build artifact and is simply absent until the first `npm run build:css`.

    The URL is built by hand rather than through `url_for`, matching what the
    templates already did, and so ignores `root_path`. It takes literals from
    templates and never anything a user typed."""
    try:
        version = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version:x}"


templates.env.globals["static_url"] = static_url
templates.env.globals["error_hint"] = error_hint
