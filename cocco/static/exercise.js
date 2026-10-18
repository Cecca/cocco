// The exercise pages' editor: the student page's and the staff preview's.
//
// CodeMirror 5.65.16, vendored under static/codemirror/ (MIT, its LICENSE
// beside it) straight from the npm release: version 6 needs a bundler, and
// this repo has none. Loaded before this file, with the python mode and the
// two bracket addons.
//
// - `<textarea data-editor>` becomes an editor. The textarea stays the form
//   field: every change is copied back into it, so an htmx Run or Check posts
//   what is on screen.
// - `data-draft-key` on a textarea (the editor's or a plain one) keeps its
//   contents in localStorage under that key, and opens on them when there are
//   any. When there are none, what the server rendered stays - the last code
//   sent from this exercise, or the starter. Storage can be missing or throw
//   (a private window, blocked site data), so every access is guarded and the
//   page works without it.
// - Ctrl-Enter clicks the element named by `data-run`, Shift-Ctrl-Enter the
//   one named by `data-check`, if they are there and enabled.
// - `[data-hints]` reveals its `[data-hint]` children one at a time, with the
//   count kept under `data-draft-key` as well. Hints are not secret.
// - `coccoSetCode(id, text)` puts code in a textarea's editor, or in the
//   textarea itself while there is no editor (the script failed to load).
// - `[data-swap-guard]`: a response that is not a 2xx is never swapped in for
//   one of these. htmx 4 swaps error bodies, so a session deleted under a
//   polling strip, or a student unenrolled mid-session, would otherwise have a
//   404's JSON or the 403 page land in the strip or a result box. A polling
//   element is frozen where it is instead - a disconnected copy stops its
//   `every` - with a line saying why; a button gets the line in its target.
//   Local to these elements: htmx's own configuration is left alone.
(function () {
  "use strict";

  function load(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function store(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      // Full, blocked or absent: the draft simply is not kept.
    }
  }

  function click(id) {
    var button = id && document.getElementById(id);
    if (!button || button.disabled) return;
    // A button on a tab that is not showing: show it, so the result is seen.
    var panel = button.closest(".tab-content");
    var tab = panel && panel.previousElementSibling;
    if (tab && tab.matches("input.tab")) tab.checked = true;
    button.click();
  }

  function restore(textarea) {
    var key = textarea.dataset.draftKey;
    if (!key) return;
    var draft = load(key);
    if (draft !== null) textarea.value = draft;
  }

  function upgrade(textarea) {
    restore(textarea);
    var key = textarea.dataset.draftKey;
    var run = function () { click(textarea.dataset.run); };
    var check = function () { click(textarea.dataset.check); };
    var editor = CodeMirror.fromTextArea(textarea, {
      mode: "python",
      lineNumbers: true,
      indentUnit: 4,
      tabSize: 4,
      indentWithTabs: false,
      matchBrackets: true,
      autoCloseBrackets: true,
      viewportMargin: Infinity,
      // "nocursor": read, select and copy, with no caret pretending otherwise.
      readOnly: textarea.readOnly ? "nocursor" : false,
      extraKeys: {
        "Ctrl-Enter": run,
        "Cmd-Enter": run,
        "Shift-Ctrl-Enter": check,
        "Shift-Cmd-Enter": check,
        // Spaces, never a tab character: Python minds the difference.
        Tab: function (cm) {
          if (cm.somethingSelected()) cm.indentSelection("add");
          else cm.replaceSelection(" ".repeat(cm.getOption("indentUnit")));
        },
        "Shift-Tab": function (cm) { cm.indentSelection("subtract"); },
      },
    });
    editor.on("change", function () {
      editor.save();
      if (key) store(key, editor.getValue());
    });
    // For a button that puts code in the editor (the preview's "put the
    // solution in"): the editor, not the hidden textarea, is what to set.
    textarea.coccoEditor = editor;
  }

  function keep(textarea) {
    restore(textarea);
    var key = textarea.dataset.draftKey;
    textarea.addEventListener("input", function () { store(key, textarea.value); });
  }

  function hints(box) {
    var key = box.dataset.draftKey;
    var all = box.querySelectorAll("[data-hint]");
    var button = box.querySelector("[data-hint-button]");
    var shown = parseInt((key && load(key)) || "0", 10) || 0;
    function render() {
      all.forEach(function (hint, i) { hint.hidden = i >= shown; });
      if (!button) return;
      button.hidden = shown >= all.length;
      button.textContent = all.length === 1
        ? "Show a hint"
        : "Show a hint (" + (shown + 1) + " of " + all.length + ")";
    }
    if (button) {
      button.addEventListener("click", function () {
        shown = Math.min(shown + 1, all.length);
        if (key) store(key, String(shown));
        render();
      });
    }
    render();
  }

  window.coccoSetCode = function (id, text) {
    var textarea = document.getElementById(id);
    if (!textarea) return;
    if (textarea.coccoEditor) textarea.coccoEditor.setValue(text);
    else textarea.value = text;
  };

  function lostContact(status) {
    var note = document.createElement("p");
    note.className = "text-xs text-warning mt-1";
    note.textContent = "This stopped updating (the server answered " + status +
      "). Reload the page.";
    return note;
  }

  document.addEventListener("htmx:before:swap", function (event) {
    var ctx = event.detail && event.detail.ctx;
    var source = ctx && ctx.sourceElement;
    var status = ctx && ctx.response && ctx.response.status;
    if (!source || !source.closest || !source.closest("[data-swap-guard]")) return;
    if (!status || (status >= 200 && status < 300)) return;
    event.preventDefault();
    var polling = source.closest("[data-swap-guard]");
    if ((polling.getAttribute("hx-trigger") || "").indexOf("every") !== -1) {
      var frozen = polling.cloneNode(true);
      frozen.removeAttribute("hx-get");
      frozen.removeAttribute("hx-trigger");
      frozen.removeAttribute("data-swap-guard");
      frozen.appendChild(lostContact(status));
      polling.replaceWith(frozen);
    } else {
      var target = document.querySelector(polling.getAttribute("hx-target"));
      if (target) target.replaceChildren(lostContact(status));
    }
  });

  document.querySelectorAll("textarea[data-editor]").forEach(upgrade);
  document.querySelectorAll("textarea[data-draft-key]:not([data-editor])").forEach(keep);
  document.querySelectorAll("[data-hints]").forEach(hints);
})();
