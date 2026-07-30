# Engram documentation

[Francais](../fr/index.md) | [English](index.md)

Engram is the local operational memory in the Datacron, Cortex, and Engram trilogy.

## I want to...

| Goal | Open |
| --- | --- |
| Start Engram without reading everything | [Five-minute quick start](quick-start.md) |
| Use `recall` and `remember` every day | [User guide](user-guide.md) |
| Choose between Engram, Datacron, and Cortex | [Trilogy guide](datacron-cortex.md) |
| Connect Claude, Codex, or Gemini | [Setup](setup.md) |
| Install automatic client behavior | [Client protocol](client-protocol.md) |
| Attest, migrate, reindex, or consolidate | [Operator guide](operator-guide.md) |
| Fix a specific symptom | [FAQ](faq.md) |

If you do not know which one to choose, open only the
[five-minute quick start](quick-start.md).

## Short path

```text
Five-minute quick start
  -> User guide
  -> Engram / Datacron / Cortex guide
```

The [operator guide](operator-guide.md) is needed only to change trust, the database, or the
Datacron vault.

## Technical references

These pages help readers understand or audit the product. They are not required to start.

| Reference | Contents |
| --- | --- |
| [Data contract](spec.md) | Types, fields, provenance, lifecycle, TTL, and freshness |
| [Architecture](architecture.md) | SQLite, HTTP MCP, retrieval, capsule, and Datacron gateway |
| [Security](security.md) | Trust boundaries, quarantine, and confinement |
| [Windows and SQLite](installation-windows.md) | SQLite runtime upgrade |
| [README](../../README.en.md) | Overview and release reference |
| [Changelog](../../CHANGELOG.md) | CalVer release history |
| [Third-party notices](../../THIRD_PARTY_NOTICES.md) | Dependencies and licenses |

## Reading rule

- Follow one procedure at a time.
- Stop at the first missing **Expected result**.
- Use the FAQ before changing several settings.
- Never run an operator command on an existing database without a backup.
