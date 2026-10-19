---
status: draft
---

Commit `c85a93d` patched the judge so that when starting the isolate box file names to be copied in the directory are no longer guessed from argument names.
The reason is that when running the judge on systemd the base directory in which it executes is `/`. The implication is that a student naming a file `run` would bring the entire `/run` directory, with all its sensitive data, in the box and be able to run it.

To me the simpler solution is to simply check that the judge starts in a non-root directory, possibly empty. We should revisit this implementation choice, possibly retaining the older behavior.
