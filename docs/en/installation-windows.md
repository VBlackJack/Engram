# Windows and SQLite installation

[Francais](../fr/installation-windows.md) | [English](installation-windows.md)

> **Use this page only when** the SQLite check in the
> [quick start](quick-start.md) reports a version below `3.51.3`.<br>
> **Verified on:** 2026-08-01.

## Why this is a hard requirement

Engram uses SQLite in WAL mode. SQLite documents a WAL-reset corruption bug in versions 3.7.0
through 3.51.2, fixed in 3.51.3 and later maintained branches. Engram fails closed before migration
when Python loads an older version.

Reference: [WAL-reset bug](https://sqlite.org/wal.html#walreset).

## The only turnkey method: uv-managed Python

This is not a preference between two supported paths. Measured on Windows:

| Distribution | SQLite linked | Clears `3.51.3` |
| --- | --- | --- |
| python.org 3.12.10 | 3.49.1 | no |
| python.org 3.13.6 | 3.50.4 | no |
| python.org 3.14.6 | 3.50.4 | no |
| uv-managed 3.13.12 | 3.50.4 | no |
| uv-managed 3.14.3 | 3.53.3 | yes |
| uv-managed 3.14.4 | 3.50.4 | no |
| **uv-managed 3.14.6** | **3.53.1** | **yes** |

**The SQLite a runtime links is decided per build, not per Python version.** The 3.14.4 row is not a
typo: that build went back below the floor before later ones went above it, and the same version
number links different SQLite versions on different operating systems. Never infer the linked
version from the Python version, on any platform. Run the check.

Every other path on this page repairs a runtime that would otherwise fail. Install the working one
instead; it does not modify an existing runtime:

```powershell
uv python install 3.14.6
uv sync --python 3.14.6
uv run --python 3.14.6 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

Installing `3.14.6` needs `uv` 0.12.1 or newer; earlier releases only know builds up to `3.14.3`.

The command must display SQLite `3.51.3` or newer (the build tested for this release displays
`3.53.1`). Use the same `--python 3.14.6` for `serve` and other commands. Contributors install
`--extra dev` separately before running tests.

**`pip install` succeeding proves nothing here.** It succeeds on every distribution in the table
above, including the four that cannot run Engram. The version check is the only signal.

The requirement is on SQLite, not on Python: a 3.13 runtime whose DLL has been replaced works, which
is why the package does not refuse to install on 3.13.

## Repairing an existing runtime: SQLite DLL replacement

Use this only when you must keep a Windows CPython runtime you already have, and only when its
`_sqlite3.pyd` loads a separate `sqlite3.dll`. Prefer the uv-managed interpreter above whenever you
are free to choose. Never replace a file while a Python process is running.

1. Identify the interpreter and loaded version:

   ```powershell
   python -c "import sys, sqlite3, _sqlite3; print(sys.executable); print(_sqlite3.__file__); print(sqlite3.sqlite_version)"
   ```

2. Close Engram, Python, and IDEs using that runtime.
3. Download `sqlite-dll-win-x64-3530300.zip` (or `win-arm64` for that architecture) from the
   [official SQLite download page](https://www.sqlite.org/download.html). For 3.53.3 x64, verify
   the published SHA3-256:
   `3a494861ce24d1f330efbc6c3fb58ce4972f2cf8df4e43122246ed987109dc8a`.
4. Locate the runtime's `sqlite3.dll`, usually beside `python.exe` or in its `DLLs` directory. Copy
   it to `sqlite3.dll.backup-<version>`.
5. Extract the archive and replace only that `sqlite3.dll`, preserving the runtime's x64/ARM64
   architecture. Do not copy it into `System32`, and do not modify a shared Python installation
   without administrator approval.
6. Open a new terminal and verify:

   ```powershell
   python -c "import sqlite3; print(sqlite3.sqlite_version); assert sqlite3.sqlite_version_info >= (3, 51, 3)"
   ```

If Python no longer starts or still loads the old version, restore the backup and use the
uv-managed runtime. Some builds link SQLite statically; a DLL cannot upgrade them, so the
runtime itself must be replaced.

This repair is exercised on every continuous-integration run, on a stock Windows runtime that has
just been observed to fail, so the steps above stay verified rather than merely written down.

The Python command in step 6 is the non-mutating check. `engram reindex` is a maintenance operation
that requires the daemon to be stopped; use it only from the
[operator guide](operator-guide.md#reindex-engram).

## HTTP server

After verification, keep:

```toml
[server]
host = "127.0.0.1"
port = 8377
path = "/mcp"
```

Allow the Python process through the firewall only for the necessary profile and interface. A
localhost server requires no inbound LAN port opening.
