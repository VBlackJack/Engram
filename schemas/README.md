# Vendored schemas

Third-party schemas Engram validates its own published metadata against. Nothing here is authored
by this project, and nothing here ships in the distribution.

They are vendored rather than fetched so the gate keeps working without a network, and so a change
upstream shows up as a reviewable diff instead of as a build that used to pass.

## `mcp-server-2025-12-11.schema.json`

| | |
| --- | --- |
| Source | <https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json> |
| Fetched | 2026-08-01 |
| Size | 22090 bytes |
| SHA-256 | `3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0` |
| Draft | JSON Schema draft-07 |
| External `$ref` | none, so validation resolves entirely offline |

Validates [`../server.json`](../server.json), the manifest submitted to the MCP registry.

**What it does not catch.** `registryType` carries examples rather than an enum, so a registry name
that resolves nowhere still validates. A passing gate means the manifest is well formed, not that
the registry will accept it; submission stays the first place that decides. A test records this
rather than leaving a reader to infer a guarantee the schema never made.

**Refreshing it is one change, not two.** The version is dated in both the file name and the
`$schema` field of `server.json`, and a test requires the two to agree. Dropping in a newer schema
without updating `server.json` fails, and pointing `server.json` at a newer schema without vendoring
it fails too. Neither can be validated against a copy it was not written for.
