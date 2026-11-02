---
title: The command line
audience: staff
order: 40
---

The `cocco` command pulls a challenge or an exercise set down to your computer
as a folder, and pushes it back after you edit it. The statement becomes a
Markdown file you can keep in git, reordering test cases is moving a few lines
in a text file, and a hundred test files go up in one push instead of a
hundred uploads.

## API tokens {#tokens}

The command line signs in with an **API token**, not with your browser. Make
one under **API tokens** on your profile page: type what it is for in **What
is it for?** (say, "laptop") and press **Create**.

The token is shown once, right after you create it. Copy it then: cocco keeps
only a fingerprint of it, so nobody can show it to you again. If you lose it,
revoke it and create another.

A token acts as you, on everything you can reach. Keep it as you would a
password, and don't put it in a file you share or commit. The list shows when
each token was created and last used, so a token marked "never" or unused for
months is probably safe to remove. **Revoke** stops it working immediately.

Give the token to the command line through the environment:

```
export COCCO_TOKEN=…
```

The command line can only change challenges and exercise sets on courses where
you are staff. Anything else is refused, whatever the token.

## Installing and setting up {#install}

`cocco` comes with cocco's own Python package (Python 3.13 or later). Install
that package with a tool such as `uv tool install` or `pipx install`, pointing
it at a copy of cocco's source; ask whoever runs your server where to find
it. Inside a copy of the source, `uv run cocco` works without installing
anything.

Tell it which server to talk to, and the token from
[API tokens](/docs/cli#tokens):

```
export COCCO_URL=https://cocco.example.org
export COCCO_TOKEN=…
cocco ls
```

Both can also be given on each command as `--url` and `--token`, but the
environment keeps the token out of your shell history. To avoid retyping the
address, put `url = "https://cocco.example.org"` in
`~/.config/cocco/cli.toml`. Without any of these, `cocco` talks to
`http://localhost:8000`.

"Not signed in" means the token is missing, wrong or revoked.

## Pulling and pushing {#ls-pull-push}

`cocco ls` lists, course by course, the challenges and exercise sets you can
edit, with their ids. Hidden, scheduled and archived ones are listed too, with
a note.

`cocco pull 12` downloads challenge 12 into a new folder named after it, such
as `challenge-12-two-sum/`. Edit the files (see
[The challenge folder](/docs/cli#zip-format)), then, inside the folder:

```
cocco push
```

The folder remembers which server and challenge it came from, so inside it
neither command needs the id. `cocco pull` there brings in what changed on
the web since. A file you changed and the web didn't is kept, a file the web
changed and you didn't is updated, and a file changed on both sides stops the
pull with nothing written.

A push is refused if the challenge changed on the web since your last pull or
push, so you can't overwrite a colleague's edit by accident. Pull first, then
push again. `--force` overrides either refusal: on a pull it takes the
server's copy and moves yours into `.cocco/discarded/`, and on a push it
overwrites what is on the server.

Exercise sets work the same way with `--set`: `cocco pull --set 5` gives you a
folder holding `exercises.md`. Pushing it imports a new version, and `cocco
push` waits for the judge to build it and prints the problems it found (up to
a minute; `--no-wait` skips the wait).

A few more:

- `cocco pull 12 --zip` downloads the challenge as a single zip file, and
  `cocco pull --set 5 --zip` the set's `.md` file.
- `-o` chooses the folder to pull into.
- `cocco push 13 path/to/folder --force` copies a folder pulled from one
  challenge onto another, replacing its contents. Create challenge 13 on the
  web first: a push never creates a challenge.

## The challenge folder {#zip-format}

A pulled challenge is a folder, and the zip `--zip` gives you holds the same
files:

- `README.md`: the problem statement, in Markdown.
- `challenge.toml`: the settings and the list of test cases.
- `tests/`: the test files, all in one folder.
- `evaluator/`: your uploaded evaluator, only if the challenge has one.

`challenge.toml` looks like this:

```toml
title = "Two sum"
ranking = "total_score"
score_threshold = 10.0
max_time_s = 2.0
max_memory_mb = 256.0
max_threads = 1
visible_from = 2026-11-09T09:00:00+01:00
evaluator = "evaluator/check.py"

[[testcase]]
input = "tests/sample.in"
expected_output = "tests/sample.out"
is_sample = true
is_active = true

[[testcase]]
input = "tests/big.in"
expected_output = "tests/big.out"
is_sample = false
is_active = true
```

- `ranking` is `running_time` or `total_score`, and `score_threshold` the
  score a submission needs to pass; leave it out for none (see
  [Leaderboard](/docs/challenges#leaderboard)). The limits are the
  [sandbox limits](/docs/challenges#limits).
- `visible_from` is when the challenge opens to students. Leave it out to keep
  the challenge hidden (see [Visibility](/docs/challenges#visibility)).
- `evaluator` names a file in `evaluator/`. Leave it out to use the default
  exact match (see [The default evaluator](/docs/evaluators#default)).
- Each `[[testcase]]` block is one test case, and the blocks run in the order
  they are written: move a block to reorder. `is_sample` makes it a
  [public sample](/docs/test-cases#samples), and `is_active = false`
  [retires it](/docs/test-cases#active).

A test case is known by its **input file name**. Renaming the input replaces
the test case with a new one. Deleting a block retires the test case if any
submission has been graded on it, and deletes it otherwise, so a push never
throws away a score.

Everything is checked before anything changes. If the push is refused, for a
missing file, a bad limit or a name used twice, the challenge stays exactly as
it was. A push can carry up to 256 MB, counted before compression. Pushing
changes no recorded scores; use [Regrade all](/docs/challenges#regrade) for
that.
