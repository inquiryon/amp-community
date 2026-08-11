# inquiryon-amp-community

AMP Community Edition governance services: human-in-the-loop decision
workflow, append-only audit history, and retryable agent-callback delivery,
built on [`inquiryon-amp-core`](https://github.com/inquiryon/amp-core).

## Get Started

### Installation

**Not yet published to PyPI** — `inquiryon-amp-core` and
`inquiryon-amp-community` must be installed from a local clone for now.
Clone both as sibling directories (`amp-community` depends on `amp-core`,
so install it first):

```bash
git clone https://github.com/inquiryon/amp-core.git
git clone https://github.com/inquiryon/amp-community.git

# Optional but recommended — keeps this off your system/global Python.
# Skip if you already have your own environment set up.
python3 -m venv .venv && source .venv/bin/activate

# amp-core first — amp-community depends on it
pip install -e ./amp-core
pip install -e ./amp-community
```

Once published, this collapses to
`pip install inquiryon-amp-core inquiryon-amp-community`. Everything below
assumes this step is already done.

**AMP is installed — what's next?** Continue to **"Run the AMP Service"**
below to get it running and connect an agent. Curious what it's actually
doing first? Skip down to **"How it works"** for the governance flow,
components, and a Python API example.

### Run the AMP Service (REST API + Operator UI)

A self-hosted HTTP deployment (stdlib only — no web framework; see
[Installation](#installation) above first):

```bash
cd amp-community
cp examples/config.example.json config.json               # set strong tokens!
python -m inquiryon_amp_community.api --config config.json
# UI at http://127.0.0.1:8080/
# OpenAPI (raw spec, not a rendered doc page) at http://127.0.0.1:8080/api/v0/openapi.json
```

`config.json` holds your real tokens, and the service writes its SQLite
state file alongside it — both land inside this git checkout but are
already gitignored, so a plain `git add .` here won't stage secrets. If
you copy this checkout elsewhere or drop it into your own repo, keep that
`.gitignore` (or add equivalent rules) rather than assuming it travels
with you.

- Authentication: `Authorization: Bearer <token>` (tokens/roles in config:
  `reviewer`, `viewer`, `service`).
- Endpoints: health/version, policy list/get/import/export/validate,
  dry-run evaluation, governed request submission (idempotent per
  `request_id`), poll for the decision, HITL list/get/resolve
  (approve/reject/modify), callback retry, audit query. The OpenAPI
  document is the SDK compatibility boundary. It's the raw spec, not a
  rendered doc page (this server is stdlib-only, no bundled UI for it) —
  paste it into [editor.swagger.io](https://editor.swagger.io) for an
  interactive, browsable view.
- UI: worktray (with structured modify + re-evaluation outcome), policy
  editor/tester, audit viewer, settings. Vanilla JS, no build step, CSP'd.
- Delivery modes: `{"callback": {"mode": "poll"}}` (agents poll
  `GET /api/v0/requests/{id}`) or `{"mode": "http", "url": ...}` (push with
  persisted retries).
- Binds `127.0.0.1` by default; a warning is printed for any other bind.
  Put a TLS-terminating reverse proxy in front before external exposure.
- Docker: `docker compose up` (see `docker-compose.yml`; config lives in
  `./data/config.json`).

#### Verify it's working

A fresh instance has no policies yet, so this stores one (as a `reviewer`
— only reviewers manage policies), submits a governed request against it
(as the `service`-role agent token), then resolves it:

```bash
curl -X PUT http://127.0.0.1:8080/api/v0/policies/invoice-governance \
  -H "Authorization: Bearer amp_k_test_CHANGE-ME-reviewer" \
  -H "Content-Type: application/json" \
  -d '{"action_governance": {"criteria": [
        {"criterion_id": "c-high-amount", "tool": "pay_invoice", "hardness": "hard",
         "evaluator": {"type": "compute", "formula": "amount > 1000"}}
      ]}}'
# -> {"policy_id": "invoice-governance", "stored": true}

# Capture hitl_id from the response so the next command needs no manual
# editing — a literal "<hitl_id>" placeholder here would be misread by
# your shell as an input redirect, not a value to fill in.
HITL_ID=$(curl -s -X POST http://127.0.0.1:8080/api/v0/requests \
  -H "Authorization: Bearer amp_k_test_CHANGE-ME-agent" \
  -H "Content-Type: application/json" \
  -d '{"policy_id": "invoice-governance", "tool": "pay_invoice", "action": "submit", "params": {"amount": 4000}}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['hitl_id'])")
echo "hitl_id: $HITL_ID"
# -> hitl_id: hitl-...

curl -X POST http://127.0.0.1:8080/api/v0/hitl/$HITL_ID/resolve \
  -H "Authorization: Bearer amp_k_test_CHANGE-ME-reviewer" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve"}'
# -> {"hitl_id": "...", "result": {"decision": "approved", ...}, "delivered": true, ...}
```

Or open the AMP Operator UI at `http://127.0.0.1:8080/` and do the same thing
by hand: submit isn't available there (that's the agent's job), but the
worktray shows pending reviews and lets you approve/reject/modify them.
**First, go to Settings and paste in the reviewer token used above**
(`amp_k_test_CHANGE-ME-reviewer`) — the UI has no token by default, so
worktray/policy actions fail with `401 unauthenticated: missing bearer
token` until you set one. The token is kept in that browser session only.

### Connect an Agent to AMP

With the service running and verified working (previous section), an
agent connects one of two ways — full integration detail lives in each
SDK's own README/examples, not duplicated here:

- **Directly via the REST API** — any language, no dependency beyond an
  HTTP client. Full reference: the OpenAPI document at
  `/api/v0/openapi.json` on your running instance.
- **Via an SDK** — typed client, handles auth headers, retries, and
  polling for you:
  - Python: [`amp-sdk-python`](https://github.com/inquiryon/amp-sdk-python)
    — see its `examples/` directory (`01_evaluate.py` through
    `06_audit_query.py`): policy evaluation, governed submission, HITL
    polling, approve/reject/modify, callback delivery, audit queries.
  - TypeScript: [`amp-sdk-ts`](https://github.com/inquiryon/amp-sdk-ts) —
    same coverage, `examples/01-evaluate.ts` through
    `06-audit-query.ts`.

Building your own agent isn't the only starting point — call these APIs
from an agent you already run, or see AMP governance wired into a real
agent end-to-end: [Hermes Agent](https://github.com/inquiryon/amp-hermes-plugin)
and [OpenClaw](https://github.com/inquiryon/amp-openclaw-plugin) both ship
an AMP governance plugin (tool-call interception, HITL prompts, policy
enforcement) that you can clone and run against your own instance.

## How it works

### The governance layer

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

### AMP Community Edition Components

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

### Python API Example

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

The Community Edition runs without RLHF workers, Stripe, enterprise
analytics, organization administration, or any proprietary service — and
never silently contacts one.

## License

Apache-2.0. Contributions require DCO sign-off (`git commit -s`).
