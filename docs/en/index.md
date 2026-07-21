# Engram documentation

[Francais](../fr/index.md) | [English](index.md)

Engram is the local operational memory in the Datacron, Cortex, Engram trilogy. This hub points to
the shortest path for each need.

## Start

| Guide | Contents |
| --- | --- |
| [Setup](setup.md) | Installation, configuration, and Claude, Codex, Gemini connections |
| [Windows and SQLite](installation-windows.md) | SQLite DLL upgrade and verification |
| [User guide](user-guide.md) | Daily use, reindexing, evaluation, and consolidation |
| [Client protocol](client-protocol.md) | Ready-to-paste session instructions |

## Understand

| Reference | Contents |
| --- | --- |
| [Data contract](spec.md) | Kinds, fields, provenance, lifecycle, TTL, and freshness |
| [Architecture](architecture.md) | SQLite, HTTP MCP, retrieval, capsule, and Datacron gateway |
| [README](../../README.en.md) | Overview, commands, and configuration |
| [Changelog](../../CHANGELOG.md) | CalVer release history |

## Security

| Guide | Contents |
| --- | --- |
| [Security model](security.md) | Trust boundaries, quarantine, and confinement |
| [FAQ](faq.md) | Symptom-based diagnostics and corrective actions |
| [Third-party notices](../../THIRD_PARTY_NOTICES.md) | Dependencies and licenses |

Start with [setup.md](setup.md), then install the [client protocol](client-protocol.md) in every
connected client. Without this protocol the MCP transport works, but Engram cannot know when to
capture or recall context.
