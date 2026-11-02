"""The documentation: `cocco/docs/*.md`, its routes, and the tests that hold it
to the templates that point at it.

The helpers (`DOCS`, `help_calls`, `doc_links`) are module-level so a later
drift test (every language named, every ranking explained) is one function."""

import re
import subprocess
import sys

import pytest

from cocco.evaluators import BUNDLED
from cocco.languages import load_languages
from cocco.model import LeaderboardRanking
from cocco.schema import EvaluatorResult
from cocco.web import docs
from cocco.web.templating import render_markdown  # noqa: F401  (the filter stays separate)

ADMIN = "matteo.ceccarello@unipd.it"

TEMPLATES = docs.DOCS_DIR.parent / "templates"

_HELP_CALL = re.compile(r"""help\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']""")
_DOC_LINK = re.compile(r"\(/docs/([A-Za-z0-9_-]+)#([A-Za-z][\w-]*)\)")

DOCS = docs.load_docs()


def heading_ids(doc: docs.Doc) -> list[str]:
    return [h.id for h in docs.headings(doc.body) if h.id]


def help_calls() -> list[tuple[str, str, str]]:
    """(template, topic, section) for every `help("…", "…")` in the templates."""
    found = []
    for path in sorted(TEMPLATES.rglob("*.jinja")):
        if path.name == "_help.jinja":
            continue
        for topic, section in _HELP_CALL.findall(path.read_text(encoding="utf-8")):
            found.append((path.name, topic, section))
    return found


def doc_links() -> list[tuple[str, str, str]]:
    """(topic file, linked topic, linked id) for every `(/docs/topic#id)` link."""
    return [
        (doc.topic, topic, section)
        for doc in DOCS
        for topic, section in _DOC_LINK.findall(doc.body)
    ]


def test_the_docs_ship_inside_the_package():
    assert docs.DOCS_DIR.is_dir()
    assert docs.DOCS_DIR.parent.name == "cocco"
    assert docs.topic_files()


@pytest.mark.parametrize("doc", DOCS, ids=lambda doc: doc.topic)
def test_every_file_has_front_matter_and_renders(doc):
    assert doc.title
    assert doc.audience in docs.AUDIENCES
    assert str(docs.render_doc(doc.body)).strip()


@pytest.mark.parametrize("doc", DOCS, ids=lambda doc: doc.topic)
def test_heading_ids_are_unique_within_a_file(doc):
    ids = heading_ids(doc)
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "template, topic, section", help_calls(), ids=lambda value: str(value)
)
def test_every_help_call_resolves(template, topic, section):
    doc = docs.load_doc(topic)
    assert doc is not None, f"{template}: no cocco/docs/{topic}.md"
    assert section in heading_ids(doc), f"{template}: no {{#{section}}} in {topic}.md"


_TEMPLATE_DOC_LINK = re.compile(
    r"""url_for\(\s*['"]get_doc['"]\s*,\s*topic\s*=\s*['"]([^'"]+)['"]\s*\)\s*\}\}#([\w-]+)"""
)


def template_doc_links() -> list[tuple[str, str, str]]:
    """(template, topic, section) for every `url_for('get_doc', topic=…) }}#id`,
    the plain links an empty state uses where a ? would be out of place."""
    return [
        (path.name, topic, section)
        for path in sorted(TEMPLATES.rglob("*.jinja"))
        for topic, section in _TEMPLATE_DOC_LINK.findall(path.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize(
    "template, topic, section", template_doc_links(), ids=lambda value: str(value)
)
def test_every_template_link_to_the_docs_resolves(template, topic, section):
    doc = docs.load_doc(topic)
    assert doc is not None, f"{template}: no cocco/docs/{topic}.md"
    assert section in heading_ids(doc), f"{template}: no {{#{section}}} in {topic}.md"


def test_there_is_at_least_one_template_link_to_the_docs():
    assert template_doc_links()


def test_there_is_at_least_one_help_call():
    # A broken regex would make the parametrized test above vanish silently.
    assert help_calls()


@pytest.mark.parametrize("name", ["exercise-projector.jinja", "f-exercise-projector.jinja"])
def test_the_projector_has_no_help_buttons(name):
    # The class sees that screen; its section is reached from the dashboard's ?.
    assert "help(" not in (TEMPLATES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("source, topic, section", doc_links(), ids=lambda value: str(value))
def test_every_internal_link_resolves(source, topic, section):
    doc = docs.load_doc(topic)
    assert doc is not None, f"{source}.md links to a missing topic {topic}"
    assert section in heading_ids(doc), f"{source}.md links to a missing #{section}"


def section_text(topic: str, section_id: str) -> str:
    doc = docs.load_doc(topic)
    assert doc is not None
    source = docs.section_source(doc.body, section_id)
    assert source is not None, f"no {{#{section_id}}} in {topic}.md"
    return source


def _ranking_labels() -> dict[str, str]:
    """value -> the label the leaderboard card shows it under, e.g.
    `running_time` -> "Running time" (the text before the dash)."""
    form = (TEMPLATES / "_challenge-leaderboard.jinja").read_text(encoding="utf-8")
    return dict(
        re.findall(r'<option value="(\w+)"[^>]*>\s*([^<—]+?)\s*—', form)
    )


@pytest.mark.parametrize("ranking", list(LeaderboardRanking), ids=lambda r: r.value)
def test_every_ranking_is_documented(ranking):
    label = _ranking_labels().get(ranking.value)
    assert label, f"_challenge-leaderboard.jinja offers no option for {ranking.value}"
    assert f"**{label}**" in section_text("challenges", "leaderboard")


@pytest.mark.parametrize("filename", BUNDLED)
def test_every_bundled_evaluator_is_documented(filename):
    title, _ = BUNDLED[filename]
    assert f"**{title}** (`{filename}`)" in section_text("evaluators", "bundled")


@pytest.mark.parametrize("language", load_languages().values(), ids=lambda spec: spec.name)
def test_every_evaluator_language_is_documented(language):
    text = section_text("evaluators", "languages")
    for extension in language.extensions:
        assert f"`.{extension}`" in text


@pytest.mark.parametrize("language", load_languages().values(), ids=lambda spec: spec.name)
def test_every_submission_language_is_documented(language):
    # The name as the submission pages label it, every extension the upload
    # accepts, the program that builds or runs it, and the compiler's flags:
    # what a student needs to know their code will be treated as.
    text = section_text("submissions", "languages")
    assert f"`{language.name}`" in text
    for extension in language.extensions:
        assert f"`.{extension}`" in text
    assert f"`{language.compiler or language.interpreter}" in text
    for flag in language.compiler_args:
        if flag.startswith("-") and flag != "-o":
            assert flag in text, f"{language.name}'s {flag} is not documented"


def test_the_example_evaluator_honours_the_contract(tmp_path):
    # The one program in the docs a teacher will copy, so it has to work.
    body = section_text("evaluators", "contract")
    source = re.search(r"```python\n(.*?)```", body, re.DOTALL)
    assert source is not None
    script = tmp_path / "evaluator.py"
    script.write_text(source.group(1))
    files = {"actual": "1.0000001 2\n9\n", "expected": "1 2 3\n", "input": ""}
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    run = subprocess.run(
        [sys.executable, "-I", str(script), *(str(tmp_path / n) for n in files)],
        capture_output=True, text=True, check=True,
    )
    result = EvaluatorResult.model_validate_json(run.stdout)
    assert result.score == pytest.approx(2 / 3)
    assert result.message


def test_front_matter_is_checked():
    with pytest.raises(docs.DocError):
        docs.parse_doc("x", "no front matter\n")
    with pytest.raises(docs.DocError):
        docs.parse_doc("x", "---\ntitle: X\naudience: nobody\norder: 1\n---\n")
    with pytest.raises(docs.DocError):
        docs.parse_doc("x", "---\ntitle: X\naudience: staff\norder: first\n---\n")


def test_a_section_runs_to_the_next_heading_of_the_same_or_higher_level():
    body = (
        "## One {#one}\n\ntext\n\n### Inner {#inner}\n\ndeep\n\n"
        "```\n## not a heading\n```\n\n## Two {#two}\n\nother\n"
    )
    one = docs.section_source(body, "one")
    assert one is not None
    assert "deep" in one and "not a heading" in one and "other" not in one
    inner = docs.section_source(body, "inner")
    assert inner is not None
    assert "deep" in inner and "text" not in inner and "other" not in inner
    assert docs.section_source(body, "missing") is None


def test_explicit_ids_reach_the_html():
    html = str(docs.render_doc("## Leaderboard {#leaderboard}\n"))
    assert 'id="leaderboard"' in html


def test_the_shared_markdown_filter_does_not_take_attribute_lists():
    html = str(render_markdown("## Title {#x}\n"))
    assert 'id="x"' not in html


async def test_an_anonymous_visitor_reads_the_index(make_client):
    response = make_client().get("/docs", follow_redirects=False)
    assert response.status_code == 200
    assert "/docs/challenges" in response.text
    assert "Sign in</a>" in response.text


async def test_every_file_appears_on_the_index(make_client):
    text = make_client().get("/docs").text
    for doc in DOCS:
        assert f"/docs/{doc.topic}\"" in text


async def test_an_anonymous_visitor_reads_a_page(make_client):
    response = make_client().get("/docs/challenges")
    assert response.status_code == 200
    assert 'id="leaderboard"' in response.text
    assert 'href="#leaderboard"' in response.text


async def test_a_signed_in_user_keeps_their_navbar(make_client):
    response = make_client(as_=ADMIN).get("/docs/challenges")
    assert response.status_code == 200
    assert "Sign out" in response.text
    assert ">Docs</a>" in response.text


async def test_the_fragment_is_one_section_and_a_link_to_the_page(make_client):
    response = make_client().get("/f/docs/challenges/leaderboard")
    assert response.status_code == 200
    assert 'id="leaderboard"' in response.text
    assert 'href="/docs/challenges#leaderboard"' in response.text
    assert "<html" not in response.text


def test_the_help_macro_is_a_real_link_with_htmx_on_top():
    macro = docs.templates.env.get_template("_help.jinja").module.help  # type: ignore[attr-defined]
    html = str(macro("challenges", "leaderboard", "Help: Leaderboard"))
    assert 'href="/docs/challenges#leaderboard"' in html
    assert 'hx-get="/f/docs/challenges/leaderboard"' in html
    assert 'hx-target="#help-body"' in html
    assert 'aria-label="Help: Leaderboard"' in html


async def test_an_unknown_topic_or_section_is_404(make_client):
    client = make_client()
    assert client.get("/docs/nope").status_code == 404
    assert client.get("/f/docs/nope/leaderboard").status_code == 404
    assert client.get("/f/docs/challenges/nope").status_code == 404


async def test_a_path_traversal_slug_is_404(make_client):
    client = make_client()
    assert client.get("/docs/..%2Fabout").status_code == 404
    assert client.get("/docs/..%2Fabout.md").status_code == 404
    assert client.get("/f/docs/..%2Fabout/leaderboard").status_code == 404


async def test_the_page_renders_the_file_as_it_is_now(make_client, tmp_path, monkeypatch):
    # Read per request, so an edit shows without a restart.
    (tmp_path / "x.md").write_text(
        "---\ntitle: Ex\naudience: student\norder: 1\n---\n\n## A {#a}\n\nhello\n"
    )
    monkeypatch.setattr(docs, "DOCS_DIR", tmp_path)
    client = make_client()
    assert "hello" in client.get("/docs/x").text
    assert "For students" in client.get("/docs").text
    assert client.get("/docs/challenges").status_code == 404
