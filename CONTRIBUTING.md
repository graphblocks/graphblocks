# Contributing to GraphBlocks

Thank you for helping build GraphBlocks. Contributions may improve code,
schemas, TCK fixtures, examples, specifications, or project documentation.

## Before opening a change

1. Search existing issues and pull requests for overlapping work.
2. For a new public contract or a breaking semantic change, open a design issue
   before implementation.
3. Keep changes focused. Add or update the narrowest applicable test and
   specification section.
4. Do not add credentials, customer data, or provider secrets to fixtures.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
cargo test --workspace --all-targets
```

Run formatting and lint checks relevant to the files you changed. CI also runs
Python tests, Rust formatting/lint/test checks, documentation integrity checks,
and the example integration suite.

## Contract changes

Wire-shape changes belong in `schemas/`; semantic requirements belong in
`docs/specification/`; executable compatibility belongs in `tck/` and
`acceptance/`. Keep these authorities synchronized. New diagnostic codes must
be stable, documented, and covered by a negative test.

Conformance claims require deterministic evidence for failure and boundary
behavior, including replay, cancellation, stale identities, policy rejection,
or dependency closure when applicable.

## Pull requests

Describe the problem, the chosen contract, compatibility impact, and commands
used to verify the change. Link a design issue for substantial specification
work. By contributing, you agree that your contribution is licensed under the
repository's Apache-2.0 license. The project does not currently require a CLA.

Maintainers review changes according to [GOVERNANCE.md](GOVERNANCE.md).
