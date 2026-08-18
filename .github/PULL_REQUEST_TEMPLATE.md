## What changes for a user

<!-- What can they now do, or no longer suffer? Not which function moved. -->

## How it was verified

<!-- What did you measure, and against what? For a fix, the test that fails without it. -->

## Checklist

- [ ] A test fails without this change, and passes with it
- [ ] `ruff format --check .`, `ruff check .` and `mypy src tests ci` are clean
- [ ] `pytest` passes, and the coverage floors hold
- [ ] Documentation updated in **both** `docs/fr` and `docs/en`, if behaviour or options changed
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`
- [ ] No `# noqa`, `# type: ignore` or skipped test added without a comment saying why
