---
status: draft
---

# General refactor

The code in this project's web interface is mainly split into two files, `main.py` and `repository.py`. Both are several thousand lines long. This is not very manageable. The goal of this change is to split those two modules in smaller pieces, so that the entire project is more readable.
