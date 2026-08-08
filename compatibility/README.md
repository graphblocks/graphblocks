# Compatibility snapshots

This directory enumerates the deliberately small Python and command-line
surface proposed for the first stable GraphBlocks release. The snapshots are a
release gate: repository presence or a top-level re-export does not make an API
stable.

The stable local runtime entry points are `LocalRuntime` and
`core_stdlib_registry()`. Their result and journal types contain only terminal
C1 lifecycle state. `InProcessRuntime`, `RuntimeCheckpoint`, `RunResult`, and
the full `stdlib_registry()` continue to ship as preview APIs for higher-profile
checkpoint, callback, application, governance, and production experiments.

- `stable-python-surface.yaml` is the reviewed list of candidate-stable C0/C1
  import paths.
- `python-package-boundaries.yaml` classifies every unlisted public module as
  preview, records the lazy root-compatibility policy, and caps base
  dependencies plus cold root and canonical-leaf imports.
- `python-root-compatibility.json` freezes every transitional package-root
  runtime alias and its defining owner module without promoting it to stable.
- `stable-python-api.json` records their exact inspectable signatures and the
  public fields of dataclasses.
- `stable-runtime-surface.yaml` and `stable-runtime-api.json` freeze the
  minimal installed `graphblocks-runtime` C1 surface, its closed status and
  result contracts, and the exact signatures exercised from the selected
  native wheel. Persistence, checkpoint, callback, deployment, raw JSON, and
  test-runtime helpers remain preview.
- `stable-cli-cases.yaml` is the reviewed list of `validate`, `plan`, and `run`
  command scenarios.
- `stable-cli-contracts.json` records each scenario's exit code and parsed JSON
  stdout contract.
- `stable-testing-surface.yaml` and `stable-testing-api.json` freeze the
  candidate-stable `graphblocks-testing` TCK data and runner surface.
- `stable-testing-cli-cases.yaml` and `stable-testing-cli-contracts.json`
  freeze the installed `graphblocks-tck` discovery and execution contracts.

Run the gate after an API or CLI change:

```console
python tools/check_compatibility.py
```

An intentional compatibility change requires policy and release-note review.
After that review, regenerate the machine snapshots explicitly:

```console
python tools/check_compatibility.py --update
```

These remain candidate snapshots while the release matrix is blocked on
independent compatibility review and the external release gates. Updating a
snapshot does not by itself satisfy the first-stable-release gates.
