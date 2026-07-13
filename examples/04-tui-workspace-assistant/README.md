# TUI Workspace Assistant

This example models a terminal UI as an Application Protocol client rather than
a graph node. The application exposes run events and commands while the graph
handles workspace snapshot, agent work, review, and commit boundaries.

The root graph is assembled from two typed subgraphs. `prepare` fills the
`workspace-context` slot from `fragments/workspace-context.yaml`, and `respond`
fills the `assistant-turn` slot from `fragments/assistant-turn.yaml`. Composition
materializes them as the ordinary nodes `prepare__snapshot`,
`prepare__context`, `respond__agent`, and `respond__candidate` before validation,
planning, or execution:

```bash
graphblocks compose examples/04-tui-workspace-assistant/example.yaml
```

The portable assistant fragment always binds its agent to the logical `coding-model`
resource. Choose which physical provider supplies that resource with
`--model`:

```bash
python examples/04-tui-workspace-assistant/run.py --model gpt
python examples/04-tui-workspace-assistant/run.py --model gemini
python examples/04-tui-workspace-assistant/run.py --model claude
```

| Choice | Provider API | Model | Binding profile |
| --- | --- | --- | --- |
| `gpt` | OpenAI Responses | `gpt-5.6-sol` | `bindings/gpt.yaml` |
| `gemini` | Google Interactions | `gemini-3.5-flash` | `bindings/gemini.yaml` |
| `claude` | Anthropic Messages | `claude-sonnet-5` | `bindings/claude.yaml` |

Each profile maps the same logical resource to a provider implementation and a
`secret://` credential reference. A deployment must supply the corresponding
provider adapter and resolve that reference through its secret manager; API key
values never belong in these files.

The example runner validates the selected profile, then executes the graph with
a recording workspace API and scripted agent. The JSON output distinguishes the
selected external binding from the `offline-fixture` execution. Tests therefore
exercise all three choices without sending a provider request, opening a
terminal session, or mutating a real workspace. This repository currently ships
an OpenAI response normalizer but not complete live transports for all three
providers, so the binding profiles are deployment integration contracts rather
than a claim that the example runner calls those APIs directly.
