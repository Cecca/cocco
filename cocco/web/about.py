"""The about page: `cocco/about.md`, rendered."""

from pathlib import Path

from fastapi import APIRouter, Request

from cocco.web.access import Blocked, NotAuthenticated, get_user
from cocco.web.core import DBDep
from cocco.web.templating import templates

router = APIRouter()

# Authored in the repository and shipped inside the package, resolved like
# `templates/` and `languages.toml`: an installed wheel has no repo root.
ABOUT_PATH = Path(__file__).resolve().parent.parent / "about.md"


@router.get("/about")
async def get_about(request: Request, db: DBDep):
    """The one page besides `/login` that asks nobody to sign in: it is what a
    visitor reads to decide whether to.

    Read on every request rather than at import, so an edit shows on the next
    reload: `cocco-web --reload` watches `.py` files and would not notice it.

    The navbar still shows whoever is signed in, which is all the user is for -
    so a missing session and a blocked account both just read it anonymously.
    """
    try:
        user = await get_user(request, db)
    except (NotAuthenticated, Blocked):
        user = None
    return templates.TemplateResponse(
        request=request,
        name="about.jinja",
        context={
            "about": ABOUT_PATH.read_text(encoding="utf-8"),
            "user": user,
        },
    )
