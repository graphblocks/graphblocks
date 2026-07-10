# TUI Workspace Assistant

This example models a terminal UI as an Application Protocol client rather than
a graph node. The application exposes run events and commands while the graph
handles workspace snapshot, agent work, review, and commit boundaries.

```bash
python examples/04-tui-workspace-assistant/run.py
```

The runner executes the graph with a recording workspace API and scripted agent,
asserting resolved inputs, draft output, and journal completion. It opens no
terminal session and mutates no real workspace.
