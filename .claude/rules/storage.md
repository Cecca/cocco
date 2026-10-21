---
paths:
  - "cocco/storage.py"
  - "cocco/judge.py"
  - "cocco/main.py"
  - "cocco/transfer.py"
  - "cocco/migrate.py"
---

# Storage format

Moved from `CLAUDE.md`'s *Conventions*.

- **Everything under the storage root is gzip, under its logical name**, and only `cocco/storage.py` knows. `write`, `read`, `open`, `save_stream` take and return plaintext.
  - **No `.gz` suffix**: `testcase.input_path` stays the uploaded name used by `resolve`, `rename`, `plan_testcases`, etc. A suffix would need adding and stripping everywhere, and silently break wherever it was forgotten.
  - `save_stream` returns the **uncompressed** count (what `MAX_TESTCASE_BYTES` limits); `size` is bytes on disk.
  - Deflating is CPU, so async callers use a thread (`post_testcase`, the push's `store`, the export's zip build).
  - Files leave as decompressing **streams**, never `FileResponse` (`_stored_file_download`, `_zip_stored`).
  - No plaintext fallback: `cocco-migrate compress-storage` converts a root, `check_storage_format` refuses to start without the `.storage-format` marker (in `server.py`, the startup hook and `judge.main`), and reading a plain file raises `StorageFormatError` naming that command.
  - Bundled evaluators in `cocco/evaluators/` are source tree, so plain: `prepare_evaluator` and `get_evaluator_download` each keep that one branch.
