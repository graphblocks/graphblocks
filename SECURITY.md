# Security Policy

## Supported versions

GraphBlocks is alpha software. Security fixes are applied to the current
development branch; no released maintenance series is supported yet. Do not use
the reference runtime as a security boundary without an independent review.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository when
available. Otherwise contact a repository maintainer privately through the
hosting organization. Do not disclose a suspected vulnerability in a public
issue, discussion, or pull request.

Include the affected revision, configuration, reproduction steps, impact, and
any suggested mitigation. Maintainers will acknowledge a usable report, assess
severity and affected contracts, coordinate a fix, and publish an advisory when
appropriate. Response times are best effort until the project establishes a
formal security team and release cadence.

## Scope

Useful reports include authentication or authorization bypasses, secret
exposure, unsafe callback or webhook handling, tenant isolation failures,
artifact or release verification bypasses, and vulnerabilities in project-owned
code. Provider outages, unsupported deployment configurations, and findings
that require trusted local code execution may be closed as out of scope.
