"""The documentation: `cocco/docs/*.md`, rendered as an index, a page per topic
and, for the ? buttons, one section at a time."""

import re
from dataclasses import dataclass
from pathlib import Path

import markdown as md
from fastapi import APIRouter, HTTPException, Request
from markupsafe import Markup

from cocco.web.access import Blocked, NotAuthenticated, get_user
from cocco.web.core import DBDep
from cocco.web.templating import _MARKDOWN_EXTENSION_CONFIGS, templates

router = APIRouter()

# Authored in the repository and shipped inside the package, like `about.md`.
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

AUDIENCES = ("staff", "student", "everyone")

# Not `render_markdown`'s list: that filter also renders what teachers write
# (statements, course descriptions), and `attr_list` would let them put
# arbitrary attributes on an element. These files are authored in the repo.
_DOC_EXTENSIONS = ["fenced_code", "tables", "codehilite", "attr_list", "toc"]

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*(?:\{#([A-Za-z][\w-]*)\})?[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


class DocError(ValueError):
    """A file that breaks the format: no front matter, a bad audience."""


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    id: str | None
    line: int


@dataclass(frozen=True)
class Doc:
    topic: str
    title: str
    audience: str
    order: int
    body: str


def render_doc(text: str) -> Markup:
    html = md.markdown(
        text,
        extensions=_DOC_EXTENSIONS,
        extension_configs=_MARKDOWN_EXTENSION_CONFIGS,
    )
    return Markup(html)


def topic_files() -> dict[str, Path]:
    """Every topic found on disk, by slug. The only way a request's `topic`
    reaches a file: it is looked up here, never joined onto a directory."""
    return {path.stem: path for path in sorted(DOCS_DIR.glob("*.md"))}


def parse_doc(topic: str, text: str) -> Doc:
    """Split the front matter (`title`, `audience`, `order`) from the body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DocError(f"{topic}: the file must start with a front matter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise DocError(f"{topic}: the front matter block is never closed") from None
    fields = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise DocError(f"{topic}: front matter line {line!r} is not `key: value`")
        fields[key.strip()] = value.strip()
    missing = {"title", "audience", "order"} - fields.keys()
    if missing:
        raise DocError(f"{topic}: front matter lacks {', '.join(sorted(missing))}")
    if fields["audience"] not in AUDIENCES:
        raise DocError(f"{topic}: audience must be one of {', '.join(AUDIENCES)}")
    if not fields["title"]:
        raise DocError(f"{topic}: the title is empty")
    try:
        order = int(fields["order"])
    except ValueError:
        raise DocError(f"{topic}: order must be a whole number") from None
    return Doc(
        topic=topic,
        title=fields["title"],
        audience=fields["audience"],
        order=order,
        body="\n".join(lines[end + 1 :]) + "\n",
    )


def load_doc(topic: str) -> Doc | None:
    path = topic_files().get(topic)
    if path is None:
        return None
    return parse_doc(topic, path.read_text(encoding="utf-8"))


def load_docs() -> list[Doc]:
    docs = [parse_doc(t, p.read_text(encoding="utf-8")) for t, p in topic_files().items()]
    return sorted(docs, key=lambda doc: (doc.order, doc.title))


def headings(body: str) -> list[Heading]:
    """Every ATX heading outside a code fence, with its `{#id}` if it has one."""
    found = []
    fence = None
    for number, line in enumerate(body.splitlines()):
        marker = _FENCE.match(line)
        if marker:
            fence = None if fence == marker.group(1) else (fence or marker.group(1))
            continue
        if fence:
            continue
        match = _HEADING.match(line)
        if match:
            found.append(Heading(len(match.group(1)), match.group(2), match.group(3), number))
    return found


def section_source(body: str, section: str) -> str | None:
    """The text from the heading with this id to the next heading of the same
    or a higher level (a smaller or equal number of `#`)."""
    all_headings = headings(body)
    for i, heading in enumerate(all_headings):
        if heading.id != section:
            continue
        lines = body.splitlines()
        end = len(lines)
        for later in all_headings[i + 1 :]:
            if later.level <= heading.level:
                end = later.line
                break
        return "\n".join(lines[heading.line : end]) + "\n"
    return None


async def _optional_user(request: Request, db):
    try:
        return await get_user(request, db)
    except (NotAuthenticated, Blocked):
        return None


@router.get("/docs")
async def get_docs(request: Request, db: DBDep):
    """Open to everyone, like `/about`: nothing in the docs is secret, and the
    navbar still shows whoever is signed in."""
    docs = load_docs()
    groups = [
        {"audience": audience, "docs": [d for d in docs if d.audience == audience]}
        for audience in AUDIENCES
    ]
    return templates.TemplateResponse(
        request=request,
        name="docs-index.jinja",
        context={
            "groups": [g for g in groups if g["docs"]],
            "user": await _optional_user(request, db),
        },
    )


@router.get("/docs/{topic}")
async def get_doc(request: Request, topic: str, db: DBDep):
    doc = load_doc(topic)
    if doc is None:
        raise HTTPException(status_code=404, detail="No such documentation page")
    contents = [h for h in headings(doc.body) if h.level == 2 and h.id]
    return templates.TemplateResponse(
        request=request,
        name="docs.jinja",
        context={
            "doc": doc,
            "html": render_doc(doc.body),
            "contents": contents,
            "user": await _optional_user(request, db),
        },
    )


@router.get("/f/docs/{topic}/{section}")
async def get_doc_section(request: Request, topic: str, section: str):
    """One section for the ? pop-up. Needs no user: nothing in it varies."""
    doc = load_doc(topic)
    source = section_source(doc.body, section) if doc else None
    if source is None:
        raise HTTPException(status_code=404, detail="No such documentation section")
    return templates.TemplateResponse(
        request=request,
        name="f-doc-section.jinja",
        context={"doc": doc, "section": section, "html": render_doc(source)},
    )
