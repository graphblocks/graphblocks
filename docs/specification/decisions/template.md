# ADR-NNNN: Decision title

- Status: proposed
- Owners: <!-- named maintainers -->
- Profiles: <!-- affected conformance profiles -->
- Target release: <!-- release or “none” -->

## Context

Describe the problem, existing contract, consumers, and constraints. Link the
evidence that makes a decision necessary.

## Scope classification

Select exactly one primary boundary:

- ☐ portable core
- ☐ extension profile
- ☐ adapter or integration
- ☐ example or pattern
- ☐ external project

## Core inclusion evidence

Complete every item even when the selected boundary is not portable core.

- ☐ Portable execution necessity: explain why portable execution semantics
      require this capability.
- ☐ Independent implementations: identify two runtimes that can implement
      the contract without sharing the same implementation.
- ☐ Provider-neutral conformance: identify the TCK cases and evidence format
      that verify behavior without a provider-specific service.
- ☐ Policy neutrality: show that the contract imposes no model provider,
      database, server-framework, credential, or deployment policy.

A portable-core decision requires all four items. Otherwise record why the
extension, adapter, example, or external-project boundary is authoritative.

## Non-goals and adapter seams

List behavior deliberately excluded from GraphBlocks and identify the
versioned bindings, request/response contracts, or profile gates through which
external hosted orchestration, API gateways, secret management, ETL, cluster
reconciliation, or other operational products connect.

## Decision

State the chosen contract and authority boundary.

## Consequences

Describe benefits, costs, compatibility effects, and operational ownership.

## Migration

Describe versioned migration, deprecation, rollback, and unsupported-state
behavior. Use “not applicable” with a reason when no migration exists.

## Conformance impact

Name affected profiles, fixtures, diagnostics, acceptance gates, and retained
evidence. Package or directory presence must not be used as a compatibility
claim.
