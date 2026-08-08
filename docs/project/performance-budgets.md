# Performance budgets

GraphBlocks keeps the executable Python performance contract in
`compatibility/python-performance-budgets.yaml`. The gate runs only on the
canonical Linux/Python 3.11 CI lane so operating-system and interpreter
variance cannot silently redefine a release threshold.

The gate covers four audit seeds:

| Benchmark | Sizes | Metric and rule |
| --- | --- | --- |
| Decimal canonicalization | 2k, 8k, 16k values | Median elapsed time, absolute caps, and normalized growth cap. |
| In-memory journal append | 4k, 16k, 64k records | Median elapsed time, absolute caps, and normalized growth cap. |
| Python compiler | 50, 200, 800 nodes | Median elapsed time, absolute caps, and normalized growth cap. |
| Retained server state | 5 and 20 completed runs | Deep retained bytes, absolute caps, and normalized growth cap. |

Elapsed-time cases use one warmup and three measured runs, collect garbage
before every observation, and evaluate the median. The server-memory case runs
once per size because it measures deterministic reachable object size rather
than allocator RSS. Every case must satisfy both its size-specific hard cap and
the normalized first-to-last growth cap. This catches an absolute slowdown and
the return of a super-linear implementation independently.

Cold-import time, RSS, loaded modules, and root API size remain enforced by the
fresh-interpreter protocol in `compatibility/python-package-boundaries.yaml`.
The performance manifest names that contract as a companion gate rather than
duplicating it in a warm process.

Run the canonical gate with:

```bash
python tools/check_performance_budgets.py \
  --report dist/ci/python-performance-budgets.json
```

The report binds the budget-file digest, environment, raw observations,
thresholds, growth decisions, and failures. CI retains it with the Python 3.11
Linux diagnostics. Threshold changes therefore require a reviewable manifest
diff; a faster local machine is not grounds to tighten them without multiple
canonical-run observations, and a slower unsupported machine is not grounds
to loosen them.
