"""Reconstructed GB-PERF-002 journal harness with a stable-list assertion."""

from __future__ import annotations

from time import perf_counter

from graphblocks.runtime import ExecutionJournal


def main() -> int:
    journal = ExecutionJournal("audit-reproduction")
    storage = object.__getattribute__(journal, "_records")
    started = perf_counter()
    for index in range(16_000):
        journal.append("node_started", {"node": f"node-{index}"})
    elapsed = perf_counter() - started
    if not isinstance(storage, list) or object.__getattribute__(journal, "_records") is not storage:
        raise SystemExit("journal append replaced its internal storage")
    if len(journal.records) != 16_000:
        raise SystemExit("journal append lost records")
    print(16_000, f"{elapsed:.6f}", "stable_list=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
