---
paths:
  - "assets/**"
  - "cocco/static/**"
  - "cocco/templates/base.jinja"
  - "package.json"
  - "cocco/web/templating.py"
---

# CSS and static files

Moved from `CLAUDE.md`'s *Conventions*.

- **CSS is a build artifact.** Edit `assets/styles.css`, never `cocco/static/styles.css`. Tailwind only emits classes it sees in `cocco/templates/**/*.jinja` and `cocco/**/*.py` (`@source`), so never assemble class names at runtime. `cocco/static/` holds only files templates name: `icon-small.png`, `favicon.ico`, `styles.css`, plus `exercise.js` and CodeMirror 5 under `codemirror/` (core, `python.js`, `clike.js` and two addons, vendored with its MIT `LICENSE`, excluded via `@source not`). Large icon sources stay in `assets/`.
- **A static URL carries its own version.** `static_url()` appends the file's mtime as `?v=`; `VersionedStaticFiles` serves versioned URLs with a year of `immutable` and unversioned ones with `no-cache` (bare `StaticFiles` caused a navbar flicker by revalidating every load).
  - The mtime is read **per render, not at import**, because `npm run watch:css` changes no `.py`. A missing file falls back to the bare path.
  - `STATIC_DIR` and the `/static` mount must name **one** directory.
  - The logo is **preloaded above the stylesheet**, and `base.jinja` binds `logo_url` once for both uses, because a one-byte href mismatch fetches it twice.
