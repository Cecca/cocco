"""Test cases and the evaluator: where their files live under the storage root."""

from fastapi import (
    Request,
)

from cocco.evaluators import BUNDLED
from cocco.repository.challenges import (
    query_challenge_testcases,
    query_evaluator_path,
)
from cocco.web.core import (
    EXTENSION_TO_LANGUAGE,
    DBDep,
)
from cocco.web.templating import (
    templates,
)


def _testcase_dir(challenge_id: int) -> str:
    return f"challenge_{challenge_id}"


async def _render_testcases(request: Request, db: DBDep, course_id: int, challenge_id: int,
                            message: str | None = None, ok: bool = True):
    """The test case list fragment. Every mutation returns this, so the table
    always reflects what is actually in the database."""
    testcases = await query_challenge_testcases(db, challenge_id)
    return templates.TemplateResponse(
        request=request,
        name="f-testcases.jinja",
        context={
            "testcases": testcases,
            "course_id": course_id,
            "challenge_id": challenge_id,
            "message": message,
            "ok": ok,
        },
    )


########################################################################
# The challenge's evaluator: the script that turns a program's output into a
# score. NULL means the bundled default; anything else is a file course staff
# uploaded, living beside the challenge's test data rather than inside it.


def _evaluator_dir(challenge_id: int) -> str:
    # A sibling of `_testcase_dir`, not a directory inside it: a test case file
    # may itself be named "evaluator", and then the two would disagree about
    # whether that name is a file or a directory. Test case paths never contain
    # a slash in the name, so these two trees cannot collide.
    return f"evaluator_{challenge_id}"


async def _evaluator_context(
    db: DBDep, course_id: int, challenge_id: int
) -> dict:
    """What `f-evaluator.jinja` needs, whether it is served on its own or
    included by the editor page."""
    path = await query_evaluator_path(db, challenge_id)
    language = None
    if path is not None:
        language = EXTENSION_TO_LANGUAGE.get(
            path.rsplit(".", 1)[-1].lower() if "." in path else ""
        )
    return {
        "course_id": course_id,
        "challenge_id": challenge_id,
        # None means the bundled default, which is not in storage and has no
        # path to show.
        "evaluator_name": None if path is None else path.rsplit("/", 1)[-1],
        "evaluator_language": language,
        "bundled": BUNDLED,
    }
