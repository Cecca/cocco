"""Render a git diff as one self-contained HTML page for code review, with an
explanatory comment on every file and notes pinned to individual hunks.

    python3 review_page.py ANNOTATIONS.json OUT.html [--repo DIR] [-- GIT-DIFF-ARGS...]

GIT-DIFF-ARGS default to `--cached` (the staged change); `main...HEAD` or a
single commit's `HASH^!` work too. The annotations file says what to write:

    {
      "title": "Page heading",
      "intro_html": "<p>One line under the heading.</p>",
      "summary_html": "<p>The idea, behaviour changes, what is not done.</p>",
      "groups": [{"id": "s2", "nav": "Short name", "heading_html": "Step 2 &middot; ..."}],
      "files": {
        "path/in/repo.py": {"group": "s2", "comment_html": "...",
                            "collapsed": false, "omit_diff": false}
      },
      "hunk_notes": {"path/in/repo.py": [["substring of the hunk", "note html"]]}
    }

Every changed file must have an entry in `files` (exit 1 otherwise, naming
them), so nothing reaches the reviewer unexplained. A hunk note is placed above
the first hunk containing its substring; a note that matches nothing is
reported and the exit status is 2, with the page still written.
"""

import argparse
import html
import json
import re
import subprocess
import sys

CSS = """
:root { --bg:#fbfaf8; --fg:#1f2328; --muted:#656d76; --card:#fff; --border:#d8dee4;
  --add:#e6ffec; --addfg:#116329; --del:#ffebe9; --delfg:#82071e; --hunk:#ddf4ff; --hunkfg:#0550ae;
  --note:#fff8c5; --notebd:#d4a72c; --accent:#0969da; --code:#f3f4f6; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0d1117; --fg:#e6edf3; --muted:#8d96a0; --card:#161b22; --border:#30363d;
  --add:#12261e; --addfg:#7ee2a8; --del:#2d1418; --delfg:#ffa198; --hunk:#121d2f; --hunkfg:#79c0ff;
  --note:#2b2611; --notebd:#9e6a03; --accent:#4493f8; --code:#1f242c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 80px; }
h1 { font-size: 26px; margin: 8px 0 4px; } h2 { font-size: 20px; margin: 40px 0 8px; padding-top: 8px; border-top: 1px solid var(--border); }
p, li { max-width: 80ch; } .muted { color: var(--muted); }
code { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background: var(--code); padding: 1px 4px; border-radius: 4px; }
nav { position: sticky; top: 0; background: var(--bg); padding: 8px 0; border-bottom: 1px solid var(--border); z-index: 2;
  display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 14px; }
nav a { color: var(--accent); text-decoration: none; } nav a:hover { text-decoration: underline; }
.file { background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin: 16px 0; overflow: hidden; }
.file > header { padding: 10px 14px; border-bottom: 1px solid var(--border); display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.file > header .path { font: 600 14px ui-monospace,Menlo,monospace; word-break: break-all; }
.stat .a { color: var(--addfg); } .stat .d { color: var(--delfg); } .stat { font: 13px ui-monospace,monospace; margin-left: auto; }
.tag { font-size: 12px; padding: 1px 8px; border-radius: 10px; border: 1px solid var(--border); color: var(--muted); }
.comment { padding: 10px 14px; border-bottom: 1px solid var(--border); }
.comment p, .comment ul { margin: 6px 0; } .comment ul { padding-left: 20px; }
.q { border-left: 3px solid var(--notebd); padding-left: 8px; color: var(--muted); }
details > summary { cursor: pointer; padding: 6px 14px; color: var(--muted); font-size: 13px; }
.diff { overflow-x: auto; font: 12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
.diff table { border-collapse: collapse; width: 100%; }
.diff td { padding: 0 10px; white-space: pre; vertical-align: top; }
.diff td.n { color: var(--muted); text-align: right; user-select: none; width: 1%; padding: 0 6px; }
tr.add td.t, tr.add td.n { background: var(--add); } tr.del td.t, tr.del td.n { background: var(--del); }
tr.hunk td { background: var(--hunk); color: var(--hunkfg); }
tr.note td { white-space: normal; background: var(--note); border-top: 1px solid var(--notebd); border-bottom: 1px solid var(--notebd);
  font: 14px/1.5 system-ui,sans-serif; padding: 8px 14px; }
tr.note td p { margin: 4px 0; }
.summary { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 6px 18px; }
"""

_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_STATUS = {"A": "added", "D": "deleted", "M": "modified", "T": "type changed"}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout


def render_diff(text, notes, unplaced):
    """`git diff` output for one file as table rows, `notes` pinned above the
    first hunk containing their key; the keys that matched nothing go into
    `unplaced`."""
    notes = list(notes)
    rows = []
    for hunk in re.split(r"(?m)^(?=@@ )", text)[1:]:
        lines = hunk.rstrip("\n").split("\n")
        m = _HUNK_HEADER.match(lines[0])
        old, new = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        rows.append(
            "<tr class=hunk><td class=n></td><td class=n></td>"
            f"<td class=t>{html.escape(lines[0])}</td></tr>"
        )
        for note in [n for n in notes if n[0] in hunk]:
            rows.append(f"<tr class=note><td colspan=3>{note[1]}</td></tr>")
            notes.remove(note)
        for line in lines[1:]:
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            body = html.escape(line)
            if line.startswith("+"):
                rows.append(f"<tr class=add><td class=n></td><td class=n>{new}</td><td class=t>{body}</td></tr>")
                new += 1
            elif line.startswith("-"):
                rows.append(f"<tr class=del><td class=n>{old}</td><td class=n></td><td class=t>{body}</td></tr>")
                old += 1
            else:
                rows.append(f"<tr><td class=n>{old}</td><td class=n>{new}</td><td class=t>{body}</td></tr>")
                old, new = old + 1, new + 1
    unplaced.extend(key for key, _ in notes)
    if not rows:
        return "<p class=muted style='padding:0 14px'>No textual diff (binary or mode change).</p>"
    return "<div class=diff><table>" + "".join(rows) + "</table></div>"


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("annotations")
    parser.add_argument("out")
    parser.add_argument("--repo", default=".")
    parser.add_argument("diff_args", nargs="*", help="after --; default --cached")
    args = parser.parse_args()
    diff_args = [*(args.diff_args or ["--cached"]), "--no-renames"]

    with open(args.annotations) as fp:
        ann = json.load(fp)
    files = ann.get("files", {})
    hunk_notes = ann.get("hunk_notes", {})
    groups = ann.get("groups", [])

    numstat = {}
    for line in git(args.repo, "diff", *diff_args, "--numstat").splitlines():
        added, deleted, path = line.split("\t", 2)
        numstat[path] = (added, deleted)
    status = {}
    for line in git(args.repo, "diff", *diff_args, "--name-status").splitlines():
        code, path = line.split("\t", 1)
        status[path] = _STATUS.get(code[0], code)

    if not numstat:
        sys.exit("the diff is empty")
    missing = sorted(set(numstat) - set(files))
    if missing:
        sys.exit("no comment for:\n  " + "\n  ".join(missing))
    known = {g["id"] for g in groups}
    stray = sorted(p for p, f in files.items() if p in numstat and f.get("group") not in known)
    if stray:
        sys.exit("group not in `groups` for:\n  " + "\n  ".join(stray))
    stale = sorted(set(files) - set(numstat))
    if stale:
        print("annotated but not in the diff (ignored):\n  " + "\n  ".join(stale), file=sys.stderr)

    total_a = sum(int(a) for a, _ in numstat.values() if a.isdigit())
    total_d = sum(int(d) for _, d in numstat.values() if d.isdigit())
    title = ann.get("title", "Code review")
    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p class=muted>{ann.get('intro_html', '')} <code>git diff {html.escape(' '.join(diff_args[:-1]))}</code>: "
        f"{len(numstat)} files, <span style='color:var(--addfg)'>+{total_a}</span> "
        f"<span style='color:var(--delfg)'>&minus;{total_d}</span>.</p>",
    ]
    if ann.get("summary_html"):
        out.append(f"<div class=summary>{ann['summary_html']}"
                   "<p class=muted>Yellow rows inside a diff are review notes on the hunk below them.</p></div>")
    out.append("<nav>" + "".join(f'<a href="#{g["id"]}">{g["nav"]}</a>' for g in groups) + "</nav>")

    unplaced: list[str] = []
    for group in groups:
        members = [(p, f) for p, f in files.items() if f.get("group") == group["id"] and p in numstat]
        if not members:
            continue
        out.append(f'<h2 id="{group["id"]}">{group.get("heading_html", group["nav"])}</h2>')
        for path, f in members:
            added, deleted = numstat[path]
            if f.get("omit_diff"):
                body = "<p class=muted style='padding:0 14px'>Diff omitted.</p>"
            else:
                text = git(args.repo, "diff", *diff_args, "--", path)
                body = render_diff(text, hunk_notes.get(path, []), unplaced)
            out.append(
                f"<section class=file><header><span class=path>{html.escape(path)}</span>"
                f"<span class=tag>{status.get(path, '')}</span>"
                f"<span class=stat><span class=a>+{added}</span> <span class=d>&minus;{deleted}</span></span></header>"
                f"<div class=comment>{f.get('comment_html', '')}</div>"
                f"<details{'' if f.get('collapsed') else ' open'}><summary>diff</summary>{body}</details></section>"
            )
    out.append("</main></body></html>")
    with open(args.out, "w") as fp:
        fp.write("\n".join(out))
    print(f"wrote {args.out}: {len(numstat)} files")
    if unplaced:
        print("hunk notes that matched no hunk:\n  " + "\n  ".join(unplaced), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
