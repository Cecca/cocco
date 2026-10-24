---
name: review-page
description: Build a self-contained HTML page of a git diff for code review - every changed file with an explanatory comment, grouped (e.g. by plan step), plus review notes pinned to individual hunks. Use when asked for a review page, an annotated diff, or "an html file listing the changes with comments" for the staged change, a branch or a commit range.
---

# Review page

`review_page.py` (next to this file) renders the diff; you write the commentary as an annotations JSON file. The
script refuses to render a diff with an unexplained file, so the page always covers the whole change.

## Steps

1. **Pick the diff.** Default is the staged change (`--cached`). For a branch use `main...HEAD`, for one commit
   `HASH^!`. List it: `git diff <args> --numstat` and `--name-status`.
2. **Read every hunk you will comment on.** Comments must describe what the code does, not what a plan said it would.
   When the change came from a plan in `plans/`, read its Design and Outcome sections and group files by its steps.
3. **Write the annotations** to the scratchpad (not the repo), shaped like `example-annotations.json` here (the
   review of `plans/2026-10-23-c-exercises.md`, a good model for density and tone):
   - `title`, `intro_html` (one line: where the change comes from, test results), `summary_html` (the idea, the
     behaviour changes a reviewer must agree with, what is not done).
   - `groups`: `id`, `nav` (short, for the sticky bar), `heading_html`. Files render in group order, then in the
     order of `files`.
   - `files`: one entry per changed path: `group`, `comment_html`, optional `collapsed` (deleted files, plan files)
     and `omit_diff` (vendored or generated files; say in the comment where they came from and how they were
     verified, e.g. a checksum against upstream).
   - `hunk_notes`: `[substring, html]` pairs per path, placed above the first hunk containing the substring. Use them
     on big files (one note per function that matters), keyed on a distinctive line such as `def read_exercise_run`.
   - Put a point a reviewer should weigh (an accepted cost, a deliberate non-change) in `<p class=q>…</p>`.
   - All values are raw HTML: escape `<`, `>` and `&` in prose (`&lt;ext&gt;`), wrap identifiers in `<code>`.
4. **Render**:

   ```
   python3 .claude/skills/review-page/review_page.py ANNOTATIONS.json OUT.html [-- GIT-DIFF-ARGS]
   ```

   Exit 1 names files with no comment (or an unknown group): add them and rerun. Exit 2 means the page was written
   but some hunk notes matched nothing: fix their keys. "Annotated but not in the diff" is only a warning.
5. **Check** the page: `grep -o '<tr class=note>' OUT.html | wc -l` equals the number of notes you wrote.
6. **Report** the output path and the exact rerun command. Write it where the user asked (`/tmp/...`); offer, in one
   line, to publish it as an Artifact instead of doing so.

The page is plain HTML with inline CSS, no scripts, light and dark themes, and diffs in `<details>` so a reviewer can
fold what they have read.
