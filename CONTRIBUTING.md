# Contributing to amp-community

Thanks for your interest in contributing.

## Developer Certificate of Origin (DCO)

Every commit must be signed off, certifying you wrote the change or
otherwise have the right to submit it under this project's license
(Apache-2.0):

```
git commit -s -m "your message"
```

This adds a `Signed-off-by: Your Name <you@example.com>` trailer. PRs with
unsigned commits will be asked to amend before merge.

## Development setup

`amp-community` depends on `amp-core`. From a sibling checkout:

```
pip install -e ../amp-core -e ".[dev]"
pytest
```

## Before opening a PR

- `pytest` passes locally.
- No secrets, production URLs, or non-fictional identifiers appear
  anywhere in the diff, including tests and fixtures.
- Changes to the HITL, audit, or callback contract come with regression
  tests, not just new-feature tests — this is trust infrastructure.
- New public API surface is documented (including `api/openapi.json` if
  the REST contract changes) and, for behavior changes, reflected in
  `CHANGELOG.md`.

## Scope

`amp-community` is meant to be a clean, standalone governance runtime —
HITL, append-only audit, retryable callback delivery, a REST API, and a
basic operator UI — not a feature-flagged copy of any SaaS product built
on top of the same `amp-core` kernel.

PRs that add multi-tenancy, SaaS authentication/billing, progressive
autonomy or RLHF, model training/registry, or advanced analytics will not
be accepted here — those are Enterprise-only concepts, by design, and
integrate through `amp-core`'s `DecisionProvider` boundary in a private,
separate implementation, not inside this package.

## Reporting bugs vs. security issues

Functional bugs: open a GitHub issue. Security vulnerabilities: see
`SECURITY.md` — do not open a public issue.
