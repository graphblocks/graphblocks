# Remaining Work

This file does not maintain an independent implementation checklist. Use:

- the [deep audit remediation plan](audit-remediation-plan.md) for work order
  and milestone exit criteria;
- the [roadmap](roadmap.md) for the condensed sequence and target architecture;
- the [stable release matrix](stable-release-matrix.yaml) for the authoritative
  1.0 scope and release blockers;
- the audit issue inventory for finding-level status and regression evidence;
  and
- [implementation status](status.md) plus commit-bound CI evidence for current
  facts.

## Current release blocker

The 99-finding audit supersedes the earlier feature-led ordering. The closed
`graphblocks.ai/v1` Graph and PluginManifest resources, alpha migrations, and
candidate snapshots are implemented; recreating or re-promoting them is not
remaining work. The release is instead blocked on verified closure of every P0
and P1 plus the audit, installed-artifact, supply-chain, independent-review, and
soak gates linked above.
