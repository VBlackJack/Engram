# Windows and SQLite installation

[Francais](../fr/installation-windows.md) | [English](installation-windows.md)

## Why this is a hard requirement

Engram uses SQLite in WAL mode. SQLite documents a WAL-reset corruption bug in versions 3.7.0
through 3.51.2, fixed in 3.51.3 and later maintained branches. Engram fails closed before migration
when Python loads an older version.

Reference: [WAL-reset bug](https://sqlite.org/wal.html#walreset).

## Recommended method: uv-managed Python

This method does not modify an existing runtime:

```powershell
uv python install 3.14.3
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

The command must display SQLite `3.51.3` or newer (the build tested for this release displays
`3.53.3`). Use the same `--python 3.14.3` for `serve`, tests, and other commands.

## SQLite 3.53.x DLL method

Use this method only for a Windows CPython runtime whose `_sqlite3.pyd` loads a separate
`sqlite3.dll`. Never replace a file while a Python process is running.

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

7. Run the real guard with a local configuration:

   ```powershell
   Copy-Item engram.example.toml engram.toml
   uv run --python 3.14.3 engram reindex
   ```

If Python no longer starts or still loads the old version, restore the backup and use the
recommended `uv` runtime. Some builds link SQLite statically; a DLL cannot upgrade them, so the
runtime itself must be replaced.

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
