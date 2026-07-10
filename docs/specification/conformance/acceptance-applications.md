# Acceptance Applications

`acceptance/applications.yaml` is the executable profile-level application
manifest. It currently declares ten applications and 42 gates spanning AI
applications, governed trials, production callbacks and deployment,
orchestration, voice, and telemetry-outage correctness.

Each manifest entry binds a unique application id, profiles, a repository-local
scenario path, an ordered non-empty gate list, and a description. The runner
loads data; it MUST NOT evaluate manifest text through a shell. Built-in gates
dispatch only by exact registered name. An unknown gate fails closed unless the
caller explicitly supplies a handler for that exact name.

The shipped runner has built-ins for every gate currently declared by the
manifest, including validation/planning and all semantic probes. Structural
validation alone is insufficient.

A gate result records application, scenario, gate, status, diagnostics, and a
stable output digest. An acceptance report MUST be non-empty and bind canonical
manifest, application, and scenario digests. Evidence generated from dynamic
state must normalize fields that are explicitly non-semantic while retaining
all authority, identity, and outcome fields. Conflicting or missing evidence
fails the gate.

Required application coverage is:

- C2: direct file analysis, document ingestion, enterprise RAG, multi-turn chat;
- C3: verified RTL workspace trial;
- C4: coding-agent callbacks, Kubernetes canary, telemetry outage;
- X1: bounded research orchestration; and
- X2: realtime voice agent.

Reports MUST be regenerated when a bound digest changes. A release claim MUST
not reuse evidence from a different application, scenario, manifest, or
implementation revision.
