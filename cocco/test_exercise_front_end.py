"""What the exercise pages show for a language, and for a compiler's output:
the editor's mode and the modes it loads, the compile block of a result, the
stdin a Run was given, and the language badge. No judge runs: results are
written by hand with `finish_exercise_job`, as `test_exercise_routes.py` does.
"""

import re

from sqlalchemy import func

from cocco.conftest import sign_in_as
from cocco.exercises import format as exercise_format
from cocco.model import (
    ExerciseJob,
    ExerciseJobStatus,
    ExerciseVersionStatus,
)
from cocco.repository import judging as repo
from cocco.schema import (
    ExerciseCheckResult,
    ExerciseCompile,
    ExerciseRunResult,
)
from cocco.test_exercise_c import C_SET
from cocco.test_exercise_routes import (  # noqa: F401 - fixtures
    STUDENT,
    _import,
    _jobs,
    _versions,
    client,
    ready,
    world,
)
from cocco.test_exercise_sessions import _opened


def _ready_c(client, world, db):
    """A built C set: (set_id, version_id, preview base url)."""
    response = _import(client, world["course"], C_SET)
    assert response.status_code == 204, response.text
    set_id = int(re.search(r"/exercises/(\d+)$", response.headers["HX-Redirect"])[1])
    version_id = _versions(db, set_id)[0].id
    document = exercise_format.parse(C_SET).document
    with db.begin() as conn:
        repo.finish_version_build(
            conn, version_id, ExerciseVersionStatus.ready, [], document.model_dump(mode="json")
        )
    return set_id, version_id, f"/courses/{world['course']}/exercises/{set_id}/versions/{version_id}/preview"


def _finish(db, job_id, result) -> None:
    """Finish a job with `result`: a model, or the dict a row may hold from
    before the model changed."""
    stored = result if isinstance(result, dict) else result.model_dump(mode="json")
    with db.begin() as conn:
        conn.execute(
            ExerciseJob.update()
            .where(ExerciseJob.c.id == job_id)
            .values(status=ExerciseJobStatus.running, started_at=func.now())
        )
        repo.finish_exercise_job(conn, job_id, stored)


def _fragment(client, world, set_id, job_id) -> str:
    response = client.get(f"/f/courses/{world['course']}/exercises/{set_id}/jobs/{job_id}")
    assert response.status_code == 200
    return response.text


########################################################################
# The editor


async def test_a_c_preview_loads_clike_and_sets_the_mode(client, world, db):
    _, _, base = _ready_c(client, world, db)
    page = client.get(f"{base}/1")
    assert "codemirror/clike.js?v=" in page.text
    assert "codemirror/python.js?v=" in page.text
    assert 'data-mode="text/x-csrc"' in page.text


async def test_a_python_preview_carries_the_python_mode(client, ready):
    _, _, base = ready
    page = client.get(f"{base}/1")
    assert 'data-mode="python"' in page.text
    assert "codemirror/clike.js?v=" in page.text  # every vendored mode loads


async def test_the_student_page_carries_the_sessions_mode_and_the_prompt_note(
    client, world, db
):
    set_id, _, _ = _ready_c(client, world, db)
    session_id = _opened(client, world, set_id)
    sign_in_as(client, STUDENT)
    page = client.get(f"/courses/{world['course']}/sessions/{session_id}/1")
    assert page.status_code == 200
    assert 'data-mode="text/x-csrc"' in page.text
    assert "Don&#39;t print a prompt when reading input" in page.text or (
        "Don't print a prompt when reading input" in page.text
    )


async def test_a_python_student_page_carries_the_python_mode(client, world, ready):
    set_id, _, _ = ready
    session_id = _opened(client, world, set_id)
    sign_in_as(client, STUDENT)
    page = client.get(f"/courses/{world['course']}/sessions/{session_id}/1")
    assert 'data-mode="python"' in page.text


########################################################################
# A compiler's output


async def test_a_failed_compile_shows_its_diagnostics(client, world, db):
    set_id, _, base = _ready_c(client, world, db)
    client.post(f"{base}/1/check", data={"source": "int main(void) { return x; }"})
    (job,) = _jobs(db)
    _finish(
        db,
        job.id,
        ExerciseCheckResult(
            passed=False,
            tests=[],
            compile=ExerciseCompile(ok=False, diagnostics="main.c:1:27: error: 'x' undeclared"),
        ),
    )
    text = _fragment(client, world, set_id, job.id)
    assert "It does not compile." in text
    assert "main.c:1:27: error: &#39;x&#39; undeclared" in text
    assert "Compiler warnings" not in text
    assert "0 of 0 tests passed" not in text


async def test_a_failed_compile_of_a_run_shows_its_diagnostics(client, world, db):
    set_id, _, base = _ready_c(client, world, db)
    client.post(f"{base}/1/run", data={"source": "oops", "stdin": ""})
    (job,) = _jobs(db)
    _finish(
        db,
        job.id,
        ExerciseRunResult(
            status="ok",
            compile=ExerciseCompile(ok=False, diagnostics="main.c:1:1: error: unknown type"),
        ),
    )
    text = _fragment(client, world, set_id, job.id)
    assert "It does not compile." in text
    assert "main.c:1:1: error: unknown type" in text
    assert "Transcript" not in text


async def test_compiler_warnings_are_collapsed_above_the_tests(client, world, db):
    set_id, _, base = _ready_c(client, world, db)
    client.post(f"{base}/1/check", data={"source": "int main(void) { return 0; }"})
    (job,) = _jobs(db)
    _finish(
        db,
        job.id,
        ExerciseCheckResult(
            passed=True,
            tests=[],
            compile=ExerciseCompile(ok=True, diagnostics="main.c:3: warning: unused variable"),
        ),
    )
    text = _fragment(client, world, set_id, job.id)
    assert "It does not compile." not in text
    assert "<details" in text and "Compiler warnings" in text
    assert "main.c:3: warning: unused variable" in text
    assert text.index("Compiler warnings") < text.index("Every test passed.")


async def test_a_student_sees_diagnostics_in_full(client, world, db):
    """Diagnostics are about the student's own source, so they are not
    redacted where hidden tests are."""
    set_id, version_id, _ = _ready_c(client, world, db)
    session_id = _opened(client, world, set_id)
    sign_in_as(client, STUDENT)
    base = f"/courses/{world['course']}/sessions/{session_id}"
    client.post(f"{base}/1/check", data={"source": "oops"})
    (job,) = _jobs(db)
    _finish(
        db,
        job.id,
        ExerciseCheckResult(
            passed=False,
            tests=[],
            compile=ExerciseCompile(ok=False, diagnostics="main.c:1:1: error: unknown type"),
        ),
    )
    page = client.get(f"{base}/1")
    assert "It does not compile." in page.text
    assert "main.c:1:1: error: unknown type" in page.text


async def test_a_result_stored_before_the_change_still_renders(client, world, db, ready):
    """The old shape: an `exception` with a type, MRO, line and traceback, and
    no `compile`. The name and the traceback text are both on the page."""
    set_id, _, base = ready
    client.post(f"{base}/2/run", data={"source": "x", "stdin": "hi"})
    (job,) = _jobs(db)
    _finish(
        db,
        job.id,
        {
            "kind": "run",
            "status": "ok",
            "stdout": "partial\n",
            "exception": {
                "type": "NameError",
                "mro": ["NameError", "Exception"],
                "line": 1,
                "traceback": "Traceback (most recent call last):\n  File \"main.py\", line 1\nNameError: x",
            },
        },
    )
    text = _fragment(client, world, set_id, job.id)
    assert "NameError" in text
    assert "File &#34;main.py&#34;, line 1" in text or 'File "main.py", line 1' in text
    assert "Error output" in text
    assert "raceback</summary>" not in text


async def test_a_run_shows_the_stdin_it_was_given(client, world, db, ready):
    set_id, _, base = ready
    client.post(f"{base}/2/run", data={"source": "print(input())", "stdin": "typed-in-here"})
    (job,) = _jobs(db)
    _finish(db, job.id, ExerciseRunResult(status="ok", stdout="typed-in-here\n"))
    text = _fragment(client, world, set_id, job.id)
    assert "Input given" in text
    assert text.index("typed-in-here") < text.index("Transcript")


########################################################################
# The badge


async def test_the_set_page_and_the_session_card_show_the_language(client, world, db):
    set_id, _, _ = _ready_c(client, world, db)
    _opened(client, world, set_id)
    page = client.get(f"/courses/{world['course']}/exercises/{set_id}")
    assert re.search(r'badge[^"]*"[^>]*>\s*c\s*<', page.text)
    course = client.get(f"/courses/{world['course']}")
    assert re.search(r'badge[^"]*"[^>]*>\s*c\s*<', course.text)


async def test_a_python_set_is_badged_python(client, world, ready):
    set_id, _, _ = ready
    page = client.get(f"/courses/{world['course']}/exercises/{set_id}")
    assert re.search(r'badge[^"]*"[^>]*>\s*python\s*<', page.text)


########################################################################
# The development data


async def test_the_dev_seed_leaves_a_c_set_waiting_for_its_build(app_engine, db):
    from cocco.model import ExerciseSetVersion
    from cocco.web.seed import C_EXERCISE_SET, _populate_dummy_data

    async with app_engine.begin() as conn:
        await _populate_dummy_data(conn)
    with db.connect() as conn:
        (version,) = conn.execute(ExerciseSetVersion.select()).all()
        (job,) = conn.execute(ExerciseJob.select()).all()
    assert version.source == C_EXERCISE_SET
    assert version.status == ExerciseVersionStatus.building
    assert job.version_id == version.id and job.status == ExerciseJobStatus.queued
    parsed = exercise_format.parse(C_EXERCISE_SET, default_title="x")
    assert parsed.problems == [] and parsed.document.language == "c"
