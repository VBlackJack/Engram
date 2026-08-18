# Security policy

## Reporting a vulnerability

Please report privately, through
[GitHub security advisories](https://github.com/VBlackJack/Engram/security/advisories/new), and not
in a public issue.

Include what an attacker can reach, how to reproduce it, and the output of `engram doctor`. A
reproduction that a maintainer can run is worth more than a description of the mechanism.

Expect an acknowledgement within a few days. This is a small project with no bug bounty; what it
offers is that a real finding gets a fix, a changelog entry that names it, and credit if you want
it.

## Supported versions

The latest release receives fixes. Older releases do not: a database migrated by a newer version
cannot be opened by an older one, so there is no supported path backwards.

## What the threat model assumes

Engram is local-first, and its security rests on that. Reading these before reporting will tell you
whether what you found is a defect or the design.

- **The endpoint is loopback-only, and that is the boundary.** `server.host` accepts only
  unambiguous loopback IP literals and refuses anything else. DNS rebinding protection is on by
  default, so a foreign `Host` or `Origin` is rejected.
- **There is no authentication on the MCP endpoint.** Any process on the machine that can reach the
  port can read and write candidate memories. The MCP client identity is a namespace for pending
  observations, not a credential. Isolating the machine is the operator's job.
- **Trust is a human gesture.** Content written through the MCP tools is quarantined. Only
  `engram attest`, run by a person on the machine, promotes it. An agent cannot promote its own
  memory, deliberately.
- **The right to stop Engram is the right to write in its database directory** — which is already
  the right to corrupt it. The stop request is a file beside the ownership lock rather than a
  network verb, so nothing is exposed on the unauthenticated port.
- **The database is not encrypted.** Whatever an agent remembered is readable by anyone who can
  read the file. Put it somewhere your disk encryption covers.

Findings that interest us most: anything that promotes content to trusted without a human gesture,
anything that lets one MCP client identity read or alter another's pending observations, anything
reachable from outside loopback, and anything that grows without bound under a request an attacker
can repeat.
