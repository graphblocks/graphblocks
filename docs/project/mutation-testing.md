# Stable mutation testing

GraphBlocks treats test count and line coverage as supporting signals rather
than proof that security- and compatibility-critical branches are asserted.
The bounded stable mutation gate therefore makes a small set of deliberate
semantic faults and requires the named tests to detect every one.

The checked-in manifest is
`compatibility/stable-mutation-budget.yaml`. It currently covers one seed in
each initial stable category:

- canonical identity;
- compiler normalized-IR identity;
- explicit policy deny precedence; and
- a durable TCK case-handler branch.

Run the gate locally with:

```bash
python tools/check_mutation_coverage.py \
  --report dist/ci/stable-mutation-report.json
```

The checker copies the Python implementation and the TCK package into a
temporary tree, verifies that every exact mutation anchor occurs once and
still parses as Python, and executes only the manifest-bound pytest node IDs.
Each selector must first pass against the unmodified temporary copy, so a
stale node ID or broken baseline cannot be misreported as a killed mutant. It
never edits the working tree. A timeout is inconclusive, not a killed mutant,
and fails the gate.

The seed budget requires a 100% score and zero surviving or inconclusive
mutants. CI retains the JSON report, including manifest and source digests,
the exact test selectors, every outcome, and explicit surviving-mutant and
inconclusive-mutant inventories. New stable handlers and high-risk branches
should add a seed with a focused test before the threshold or campaign size
is raised.
