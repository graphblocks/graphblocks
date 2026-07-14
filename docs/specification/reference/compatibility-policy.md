# Compatibility and Deprecation Policy

This policy applies to surfaces classified **stable** in the
[first stable release boundary](../../project/first-stable-release.md). Preview,
internal, and reserved surfaces have the narrower promises defined below.

## Release versions and tiers

Published artifacts use Semantic Versioning. For stable artifacts:

- A patch release fixes defects without intentionally changing supported
  behavior, required dependencies, public signatures, wire output, diagnostic
  meaning/severity, or canonical bytes.
- A minor release may add backward-compatible APIs, optional wire fields with
  defined defaults, new diagnostic codes, and new opt-in behavior.
- A major release may make breaking changes after the deprecation window.

The stable Python distributions coordinate major and minor versions. A stable
`graphblocks-testing` release declares the compatible `graphblocks` range and
must not silently run a fixture set against an incompatible implementation.
Preview artifacts may change in a minor release, but breaking changes require
release notes and migrations when persisted data is affected. Preview patch
releases remain non-breaking bug-fix releases. Internal and reserved surfaces
have no compatibility promise and must not be presented as supported APIs.

## What compatibility covers

For a stable surface, compatibility includes:

- documented Python import names, call signatures, return/exception contracts,
  and typing behavior in the stable API snapshot;
- documented CLI command names, options, exit codes, and machine-readable JSON
  fields;
- accepted and emitted stable resource versions, defaults, canonical values,
  hashes, and migrations;
- registered diagnostic code, meaning, default severity, and applicability;
- TCK fixture meaning, profile requirements, report identity, and deterministic
  ordering rules;
- persisted local state that is explicitly identified as a stable storage
  format; and
- supported Python versions and platforms published for the release line.

Source-tree module discovery, underscore-prefixed names, undocumented object
attributes, test fixtures outside a claimed profile, preview modules bundled in
a stable wheel, Rust implementation crates, and textual diagnostic messages are
not stable merely because they are accessible.

## Wire and semantic changes

Stable readers must continue to accept every resource version promised for the
current major release. Additive fields are compatible only when they are
optional, have specified defaults, and cannot alter existing canonical bytes or
semantics when absent. Changing a default, field meaning, authorization result,
ordering guarantee, or canonical representation is breaking even when the JSON
Schema still accepts the document.

Removing a field, making an optional field required, narrowing accepted values,
or changing a stable canonical/hash algorithm requires a new resource version
and normally a new artifact major version. Writers emit the preferred stable
version. Migration readers must be explicit, deterministic, side-effect-free,
and covered by golden input/output and rejection tests.

Unknown `apiVersion`/`kind` pairs and unknown stable block identities fail
closed. Discovery modes may preserve unknown data only when explicitly selected
and must not produce a conformant execution plan.

## Diagnostics

The machine-readable
[diagnostic registry](diagnostic-codes.yaml) is the allocation authority. Stable
code meaning and severity are compatibility contracts. Messages may become
clearer and paths may become more precise, but automation must branch on the
code rather than message text.

A change that combines two materially different failures under one stable code,
reuses a retired code, changes an error to a warning, or removes a code without
the deprecation process is breaking. A new validation check normally receives a
new code. Security fixes may add a new error in a patch release when accepting
the input would violate an existing security or integrity invariant.

## Deprecation lifecycle

A stable surface can be deprecated only when the release that introduces the
deprecation:

1. marks it in documentation and, where practical, emits a machine-readable
   warning;
2. names the supported replacement or explains why no replacement exists;
3. provides a migration for persisted or wire-visible data; and
4. records the earliest removal version and date.

Removal occurs only in a major release and no earlier than both two consecutive
minor releases and six months after the first generally available release that
carried the deprecation. The longer interval wins. A deprecated diagnostic code
remains in the registry forever with `status: retired` after removal, so its
identifier can never be reused.

The project may bypass the normal window only to address an actively exploitable
security issue, legal requirement, or upstream platform removal that makes the
surface unsafe or impossible to support. The release notes must identify the
exception, impact, and migration or mitigation.

## Compatibility-change procedure

Every change to a stable contract must include the affected snapshot or
registry update, focused positive and negative tests, and a release-note entry.
The review states whether the change is patch-compatible, minor-compatible,
deprecated, or breaking. If reviewers cannot prove compatibility, the change is
treated as breaking or remains behind a preview surface.
