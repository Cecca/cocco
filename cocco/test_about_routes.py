"""End-to-end tests of the about page: `cocco/about.md`, open to everyone."""

from cocco.web import about

ADMIN = "matteo.ceccarello@unipd.it"


def test_the_about_file_ships_inside_the_package():
    # A wrong `Path(__file__)` hop only shows at the first request.
    assert about.ABOUT_PATH.is_file()
    assert about.ABOUT_PATH.parent.name == "cocco"


async def test_an_anonymous_visitor_reads_the_about_page(make_client):
    client = make_client()
    response = client.get("/about", follow_redirects=False)
    assert response.status_code == 200
    assert "<h1>About COCCO</h1>" in response.text
    # Nobody in the navbar, so the way in instead.
    assert "Sign in</a>" in response.text


async def test_a_signed_in_user_keeps_their_navbar(make_client):
    client = make_client(as_=ADMIN)
    response = client.get("/about")
    assert response.status_code == 200
    assert "Sign out" in response.text


async def test_the_page_renders_the_file_as_it_is_now(make_client, tmp_path, monkeypatch):
    # Read per request, so an edit shows without a restart.
    page = tmp_path / "about.md"
    page.write_text("# Hello\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    monkeypatch.setattr(about, "ABOUT_PATH", page)
    response = make_client().get("/about")
    assert "<h1>Hello</h1>" in response.text
    assert "<table>" in response.text
