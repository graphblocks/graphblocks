# TUI Workspace Assistant

This example models a terminal UI as an Application Protocol client rather than
a graph node. The application exposes run events and commands while the graph
handles workspace snapshot, agent work, review, and commit boundaries.

```bash
python examples/04-tui-workspace-assistant/run.py
```

Validation is local and does not open a terminal session or mutate a workspace.
