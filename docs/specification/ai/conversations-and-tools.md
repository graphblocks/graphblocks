# Conversations and Tools

A conversation is an ordered, revisioned record of turns, messages, tool
requests, tool results, and artifacts. Appending or changing a conversation MUST
use compare-and-swap against the expected revision. Conflicting writers MUST
fail without silently merging turns.

Assistant output begins as a draft when policy or tool execution may still
change the outcome. A draft can be committed, retracted, or replaced only under
the configured delivery policy. Committed history is immutable; branching MUST
record its parent revision and new branch identity. Retention or redaction MUST
preserve audit evidence without exposing removed content.

Tool requests in a message remain proposals until the runtime completes the
admission sequence in [tools and output policy](../core/tools-and-output-policy.md).
Tool-call identity, attempt, schema, and result identity MUST remain linked in
conversation history. Duplicate identical results may replay idempotently;
conflicting result reuse MUST fail.

Conversation memory and retrieval are explicit graph inputs. Implementations
MUST apply tenant, principal, policy, and budget boundaries before injecting
memory or external context into a model request.
