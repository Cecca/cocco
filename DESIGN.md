# Code Execution Design

## Sandbox: isolate

The sandbox for running untrusted submissions is [**isolate**](https://github.com/ioi/isolate), the tool used by IOI and ICPC. It provides strong isolation via Linux namespaces and cgroups, with built-in support for time and memory limits, stdin/stdout redirection, and termination on violation.

Isolate handles sandboxing only — it has no concept of programming languages.

## Language runtimes: Nix flakes

Language runtimes are managed via a **single Nix flake** with one package output per supported language (e.g. `python`, `gcc`, `jdk`). This gives pinned, reproducible environments managed entirely in code.

Since all Nix binaries live under `/nix/store`, the store is bind-mounted read-only into the isolate sandbox:

```bash
isolate --box-id=1 \
  --dir=/nix/store:ro \
  --stdin=/path/to/input.txt \
  --run -- /nix/store/<hash>-python3-3.11/bin/python3 solution.py
```

The exact store path for each runtime is resolved at startup and stored in the database, keeping execution fully reproducible and tied to `flake.lock`.

### NixOS setuid note

Isolate requires a setuid binary. On NixOS this is declared via `security.wrappers`. On plain Linux with Nix, isolate should be installed outside Nix (via the system package manager or manual build) while Nix manages only the language runtimes.

## Execution flow

For **compiled languages** (C, C++, Java, ...):
1. Compile outside the sandbox (trusted operation on untrusted source).
2. Run the resulting binary inside isolate.

For **interpreted languages** (Python, ...):
1. Run the interpreter directly inside isolate, with the source file copied into the sandbox working directory.

## Test case inputs

Test inputs can be several megabytes. They are stored on the **filesystem** (not in the database), with only the file path recorded in the database. Isolate accepts stdin from a file path directly, so large inputs are passed without any HTTP overhead.

## Alternatives considered

| Option | Verdict |
|---|---|
| Judge0 | Dropped: stateless HTTP API is a poor fit for multi-MB inputs |
| Docker + gVisor | Viable but heavier; container startup latency requires pre-warming |
| Firecracker microVMs | Strongest isolation, but operationally complex for a self-hosted setup |
| Piston | Easy to bootstrap, but less control and less battle-tested for adversarial input |
| Storing inputs in DB | Dropped: multi-MB blobs harm query performance, backup size, and I/O |
