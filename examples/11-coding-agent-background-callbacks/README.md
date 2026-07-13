# OpenCode-style Coding Agent with Background Callbacks

This example uses an OpenCode-shaped model/tool harness while retaining
GraphBlocks background execution, cursor replay, signed callbacks, review, and
compare-and-swap commit.

```bash
python examples/11-coding-agent-background-callbacks/run.py
```

The deterministic local harness performs the following sequence:

1. Snapshot the workspace before the model/tool step.
2. Walk upward from the working directory, select the closest `AGENTS.md` (or
   `CLAUDE.md` fallback), then combine matching `opencode.json` instructions.
3. Record model/tool parts through `pending`, `running`, `completed`, or
   `error` states.
4. Allow an in-worktree read, deny `.env`, and suspend an external-directory
   read before touching the target.
5. Resume the exact read after a fixture user chooses `once`; the approval is
   bound to the workspace snapshot and canonical tool-argument digest, so it
   cannot be reused for a sibling path.
6. Edit an ephemeral workspace copy, run a local Python syntax check, and record
   the step patch.

The example's `examples.opencode.discover_instructions@1` and
`examples.opencode.agent_session@1` are application-local custom block
contracts. The session block declares a scripted model-to-tool loop for `read`,
`edit`, and a bash-style check; message-part lifecycle; `once / always / reject`
permission responses; `external_directory: ask`; repeated-call approval; and
`.env` defaults. The checked-in Python harness is the executable reference
implementation for this example. It is not installed as a general-purpose
GraphBlocks plugin by the shared runner.

## Boundary and compatibility notes

"Workspace boundary" here is a project permission boundary, not an OS-level
sandbox. Every path is resolved before policy evaluation. In the demonstrated
read flow, a path outside the worktree emits `permission.asked` and remains
unread until an approval record matching the exact snapshot and arguments
resumes it. The local fixture executes the `read`, `edit`, and syntax-check
subset; the graph's permission contract requires the same external-directory
gate when more path-bearing tools are added. Production deployments should
still place tool execution in an OS/container sandbox when isolation is
required.

This follows OpenCode's documented [rule discovery](https://opencode.ai/docs/rules/),
[tools](https://opencode.ai/docs/tools/), and
[permission model](https://opencode.ai/docs/permissions/). The signed CI
callback, replayable GraphBlocks event journal, and compare-and-swap commit are
GraphBlocks extensions rather than claims of OpenCode API equivalence.

For deterministic offline execution, the fixture does not load global rule
files or remote `instructions` URLs. A production adapter should add OpenCode's
global precedence and five-second remote-instruction timeout.

The only subprocess is a fixed local Python syntax check. No webhook, model API,
user-supplied shell command, or external CI job is used by the fixture.
