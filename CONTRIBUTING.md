# Contributing to Engram

Thank you for considering a contribution. This page describes what the project expects from a
change so that yours lands without a round trip.

The code, its comments, its commit messages and this file are written in English. The product
documentation under `docs/` is mirrored in French and English, and both mirrors move together.

## Set up

Engram is developed with [uv](https://docs.astral.sh/uv/) and targets Python 3.13 and 3.14.

```bash
uv sync --frozen --extra dev
uv run engram --help
```

The test suite creates its own temporary databases and never touches a real one.

## Run the gates before you open a pull request

Continuous integration runs these on Ubuntu and on Windows. Running them locally first is faster
than waiting for a red badge.

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests ci
uv run pytest -q
```

Coverage is enforced twice: a project-wide floor, and a per-module floor for the modules where a
regression would be silent.

```bash
uv run pytest --cov --cov-report=term
uv run coverage json -o coverage.json
uv run python -m ci.coverage_floors --report coverage.json
```

A test run reports different totals on Windows and on Linux. That is expected: four tests need the
privilege to create a symbolic link, and four others read NTFS security descriptors. Each platform
skips what it cannot observe.

## What a change is expected to carry

- **A test that fails without it.** For a bug fix, write the test first and watch it fail against
  the current code. A test that passes before and after proves nothing.
- **Both languages, when documentation changes.** `docs/fr` and `docs/en` are mirrors. A page
  updated on one side and not the other is a defect, and the French pages use explicit ASCII
  anchors (`<a id="...">`) for headings that carry accents.
- **A changelog entry**, under `## [Unreleased]`, saying what a user can now do or no longer
  suffers — not which function changed.
- **No suppression to make a gate pass.** If `ruff` or `mypy` objects, the objection is usually
  right. `# noqa` and `# type: ignore` need a comment saying why the rule does not apply here.
  Never `--no-verify`.

## Commit messages

Conventional Commits, with a subject that says what changed for a user:

```
fix(server): free a session whose close never reached the client
```

The body explains why, and what was measured. Prefer one commit per idea over one commit per file.

## Reporting a problem

Open an issue with the output of `engram doctor`, which reports the interpreter, the SQLite
version, which configuration was resolved, the schema version and the state of the endpoint. It is
the single most useful thing you can attach.

For anything with a security dimension, read [SECURITY.md](SECURITY.md) first — please do not open
a public issue for it.
