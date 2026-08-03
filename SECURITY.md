# Security Policy

## Reporting a vulnerability

Please report suspected security vulnerabilities privately to
**team@inquiryon.com**. Do not open a public GitHub issue for suspected
vulnerabilities.

Include:

- a description of the issue and its potential impact;
- steps to reproduce (a minimal fictional example is ideal);
- the `amp-community` version and Python version in use.

We will acknowledge receipt within 5 business days and aim to provide a
remediation timeline within 10 business days of confirming the issue.

## Supported versions

This package is `0.x` (experimental `v0` contract). Only the latest `0.x`
release receives security fixes; there is no long-term-support branch at
this stage.

## Scope

In scope: the HITL workflow and authorization (`hitl.py`), append-only
audit storage (`storage.py`), the REST API server and its authentication
(`api/`), and callback delivery (`callbacks.py`).

Particularly high-value reports: anything that lets a HITL decision be
completed twice (a compare-and-set/idempotency bypass), any authorization
gap that lets a principal resolve a workitem they aren't permitted to,
audit records that can be mutated or deleted after the fact, or credential
handling issues in the bundled operator UI.

Out of scope: `amp-core` (report there — same contact), and any
Enterprise/private integration built on top of this package. This server
binds to `127.0.0.1` by default; running it exposed beyond localhost
without a TLS-terminating proxy in front is a deployment error, not an
`amp-community` vulnerability.
