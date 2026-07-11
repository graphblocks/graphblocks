# Tools and Output Policy

A tool definition describes provider-neutral input, output, and effect
contracts. A tool binding selects an implementation. Model output requesting a
tool is not authorization to execute it.

Before execution, the runtime MUST resolve the tool and schema version, validate
arguments, evaluate admission policy, obtain required approval or lease, reserve
budget, and record an execution plan. Effectful tools MUST use idempotency,
attempt, and ownership fences appropriate to the effect. Tool results MUST be
schema-validated before they become authoritative inputs to later nodes.

Streaming results MUST preserve chunk order and distinguish provisional output
from committed output. Diagnostics, artifacts, usage, and terminal status MUST
not be smuggled into an untyped text result.

Output policy is enforced at declared points such as generation chunk, client
delivery, and durable commit. Holdback bounds MUST be finite. A violation MUST
apply the configured disposition to provider cancellation, pending tool calls,
delivered drafts, and durable results. Mandatory policy checks fail closed;
optional advisory checks MUST be identified as such.

When `immediate_draft` is selected, a restored output gate MAY have client
delivery ahead of policy acceptance because the delivered text is provisional.
The restored state MUST retain the last generated, last policy-accepted, and
last client-delivered sequences independently, and later policy decisions MUST
either accept the draft or emit incomplete/retraction semantics before commit.
