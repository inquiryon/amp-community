# inquiryon-amp-community (experimental v0)

AMP Community Edition governance services: human-in-the-loop decision
workflow, append-only audit history, and retryable agent-callback delivery,
built on [`inquiryon-amp-core`](https://github.com/inquiryon/amp-core).

**Status: experimental `v0`.**

## Installation

**Not yet published to PyPI** — `inquiryon-amp-core` and
`inquiryon-amp-community` must be installed from a local clone for now.
Clone both as sibling directories (`amp-community` depends on `amp-core`,
so install it first):

```bash
git clone https://github.com/inquiryon/amp-core.git
git clone https://github.com/inquiryon/amp-community.git
pip install -e ./amp-core           # amp-community's own dependency
pip install -e ./amp-community
```

Once published, this collapses to
`pip install inquiryon-amp-core inquiryon-amp-community`. Everything below
(Quick start, REST API + operator UI) assumes this step is already done.

## The governance slice

```text
tool request
  -> deterministic policy evaluation (amp-core)
  -> decision provider under safety invariants
  -> allow, or human-review routing
  -> authorized approve / reject / modify
  -> schema validation + policy re-evaluation of modifications
  -> callback delivery with persisted, retryable delivery records
  -> complete append-only audit history
```

## Components

- `HitlService` — explicit state machine
  (`pending -> approved|rejected|modified -> resumed|closed`, plus
  `expired`/`cancelled`), idempotent creation and resolution, authorization
  separated from authentication, and safe structured modifications: a
  modified action is schema-validated, re-evaluated against policy, and
  **never executed when it fails a hard policy** — it spawns a linked
  follow-up review instead.
- `GovernanceService` — REST-neutral application service composing
  evaluation, provider decision, HITL, callbacks, and audit.
- `SqliteBackend` + stores — zero-dependency reference storage (stdlib
  `sqlite3`); implement the same interfaces for Postgres or others.
- `LocalIdentityProvider` — API-token identity with reviewer/viewer/service
  roles and opaque-scope enforcement.
- `CallbackDispatcher` + `HttpAgentCallback` — every decision creates a
  persisted delivery record with a stable `delivery_id`; failures are
  visible and retryable; retries are idempotent.
- Audit: application-level append-only event history with query by
  request/type/time and a single sanctioned retention purge. (Cryptographic
  tamper evidence is explicitly out of scope for v0.)

## Quick start

```python
from inquiryon_amp_core import InMemoryPolicyStore
from inquiryon_amp_community import build_local_service, HttpAgentCallback
from inquiryon_amp_community.identity import Principal

policies = InMemoryPolicyStore()
policies.put_policy('invoice-governance', {...})  # see amp-core README

service = build_local_service(
    policies,
    tokens={'amp_k_test_reviewer': Principal('alice', 'reviewer')},
    callback=HttpAgentCallback('http://localhost:8085/agent-callback'),
    db_path='amp-community.db',
    policy_resolver=lambda wi: policies.get_policy('invoice-governance'),
)

out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                             {'amount': 4000}, requested_by='agent:invoice-agent')
# -> {'status': 'pending_review', 'hitl_id': ...}
service.resolve('alice', out['hitl_id'], 'approve')
print(service.audit_history(out['request_id']))
```

Community runs without RLHF workers, Stripe, enterprise analytics,
organization administration, or any proprietary service — and never
silently contacts one.

## REST API + operator UI

A self-hosted HTTP deployment (stdlib only — no web framework; see
[Installation](#installation) above first):

```bash
cd amp-community
cp examples/config.example.json config.json               # set strong tokens!
python -m inquiryon_amp_community.api --config config.json
# UI at http://127.0.0.1:8080/  ·  OpenAPI at /api/v0/openapi.json
```

- Authentication: `Authorization: Bearer <token>` (tokens/roles in config:
  `reviewer`, `viewer`, `service`).
- Endpoints: health/version, policy list/get/import/export/validate,
  dry-run evaluation, governed request submission (idempotent per
  `request_id`), poll for the decision, HITL list/get/resolve
  (approve/reject/modify), callback retry, audit query. The OpenAPI
  document is the SDK compatibility boundary.
- UI: worktray (with structured modify + re-evaluation outcome), policy
  editor/tester, audit viewer, settings. Vanilla JS, no build step, CSP'd.
- Delivery modes: `{"callback": {"mode": "poll"}}` (agents poll
  `GET /api/v0/requests/{id}`) or `{"mode": "http", "url": ...}` (push with
  persisted retries).
- Binds `127.0.0.1` by default; a warning is printed for any other bind.
  Put a TLS-terminating reverse proxy in front before external exposure.
- Docker: `docker compose up` (see `docker-compose.yml`; config lives in
  `./data/config.json`).

## License

Apache-2.0. Contributions require DCO sign-off (`git commit -s`).
