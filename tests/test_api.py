"""OSS-005 API tests: the vertical-slice release gate over HTTP, plus
auth/authorization, error envelopes, OpenAPI conformance, provider-failure
fallback, and basic UI security/accessibility checks."""

import json
import os
import tempfile
import urllib.error
import urllib.request

import pytest

from inquiryon_amp_community.api import build_api_context
from inquiryon_amp_community.api.server import start_background
from inquiryon_amp_community.hitl import NotFoundError

POLICY = {
    'policy_id': 'invoice-governance',
    'schema_version': 'v0',
    'action_governance': {'criteria': [
        {'criterion_id': 'c-high-amount', 'tool': 'pay_invoice', 'action': '*',
         'hardness': 'hard',
         'evaluator': {'type': 'compute', 'formula': 'amount > 1000'}},
    ]},
}

REVIEWER = 'amp_k_test_reviewer_token'
AGENT = 'amp_k_test_agent_token'
VIEWER = 'amp_k_test_viewer_token'


@pytest.fixture()
def server():
    tmp = tempfile.mkdtemp(prefix='amp-community-test-')
    config = {
        'db_path': os.path.join(tmp, 'amp.db'),
        'policy_dir': os.path.join(tmp, 'policies'),
        'callback': {'mode': 'poll'},
        'tokens': [
            {'token': REVIEWER, 'principal': 'alice', 'role': 'reviewer'},
            {'token': AGENT, 'principal': 'agent:invoice-agent', 'role': 'service'},
            {'token': VIEWER, 'principal': 'victor', 'role': 'viewer'},
        ],
    }
    ctx = build_api_context(config)
    httpd, port = start_background(ctx)
    yield f'http://127.0.0.1:{port}'
    httpd.shutdown()


def call(base, method, path, token=None, body=None, raw=False):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload), resp.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, (payload if raw else json.loads(payload)), exc.headers


def test_release_gate_full_slice(server):
    # 1. Import a policy.
    code, out, _ = call(server, 'PUT', '/api/v0/policies/invoice-governance',
                        REVIEWER, POLICY)
    assert code == 200 and out['stored'] is True

    # 2-3. Submit a governed tool call; policy is evaluated.
    code, out, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    assert code == 200

    # 4. Reviewable outcome routed to HITL.
    assert out['status'] == 'pending_review'
    request_id, hitl_id = out['request_id'], out['hitl_id']

    # 5-6. Authorized modify: validated, re-evaluated, released when it passes.
    code, pending, _ = call(server, 'GET', '/api/v0/hitl', REVIEWER)
    assert [w['hitl_id'] for w in pending['items']] == [hitl_id]
    code, resolved, _ = call(server, 'POST', f'/api/v0/hitl/{hitl_id}/resolve',
                             REVIEWER, {'decision': 'modify',
                                        'modified_params': {'amount': 900},
                                        'note': 'reduced to limit'})
    assert code == 200
    assert resolved['result']['executed'] is True
    assert resolved['result']['params'] == {'amount': 900}

    # 7. Decision exposed to the calling agent (poll model).
    code, polled, _ = call(server, 'GET', f'/api/v0/requests/{request_id}', AGENT)
    assert code == 200
    assert polled['decision'] == 'modified'
    assert polled['executed_params'] == {'amount': 900}

    # 8. Complete append-only audit history.
    code, audit, _ = call(server, 'GET', f'/api/v0/audit?request_id={request_id}',
                          REVIEWER)
    types = [e['event_type'] for e in audit['items']]
    assert types == ['evaluation.completed', 'decision.completed',
                     'hitl.requested', 'hitl.pending', 'hitl.modified',
                     'callback.delivered', 'hitl.resumed']


def test_modify_that_fails_hard_policy_spawns_follow_up(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, out, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    _, resolved, _ = call(server, 'POST', f'/api/v0/hitl/{out["hitl_id"]}/resolve',
                          REVIEWER, {'decision': 'modify',
                                     'modified_params': {'amount': 99999}})
    assert resolved['result']['executed'] is False
    follow_up = resolved['result']['follow_up_hitl_id']
    _, wi, _ = call(server, 'GET', f'/api/v0/hitl/{follow_up}', REVIEWER)
    assert wi['status'] == 'pending'
    assert wi['policy_id'] == 'invoice-governance'  # re-review carries policy


def test_allowed_request_completes_synchronously(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, out, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 40}})
    assert out['status'] == 'allowed'


def test_auth_and_authorization(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    code, out, _ = call(server, 'GET', '/api/v0/policies')
    assert code == 401 and out['error']['code'] == 'unauthenticated'
    code, out, _ = call(server, 'GET', '/api/v0/policies', 'wrong-token')
    assert code == 401
    code, out, _ = call(server, 'PUT', '/api/v0/policies/x', VIEWER, POLICY)
    assert code == 403 and out['error']['code'] == 'forbidden'

    _, sub, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    code, out, _ = call(server, 'POST', f'/api/v0/hitl/{sub["hitl_id"]}/resolve',
                        VIEWER, {'decision': 'approve'})
    assert code == 403  # viewers cannot decide
    code, out, _ = call(server, 'POST', f'/api/v0/hitl/{sub["hitl_id"]}/resolve',
                        AGENT, {'decision': 'approve'})
    assert code == 403  # requesters cannot self-review


def test_invalid_policy_rejected_with_details(server):
    code, out, _ = call(server, 'PUT', '/api/v0/policies/broken', REVIEWER,
                        {'nonsense': True})
    assert code == 422
    assert out['error']['code'] == 'invalid_policy'
    assert out['error']['details']['errors']


def test_conflicting_resolution_is_409(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, sub, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    call(server, 'POST', f'/api/v0/hitl/{sub["hitl_id"]}/resolve', REVIEWER,
         {'decision': 'approve'})
    code, out, _ = call(server, 'POST', f'/api/v0/hitl/{sub["hitl_id"]}/resolve',
                        REVIEWER, {'decision': 'reject'})
    assert code == 409 and out['error']['code'] == 'conflict'


def test_dry_run_evaluate_and_validate(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, out, _ = call(server, 'POST',
                     '/api/v0/policies/invoice-governance/evaluate', REVIEWER,
                     {'tool': 'pay_invoice', 'action': 'submit',
                      'params': {'amount': 5000}})
    assert out['decision'] == 'require_human'
    assert out['triggered_criteria'] == ['c-high-amount']
    # Dry run created no review:
    _, pending, _ = call(server, 'GET', '/api/v0/hitl', REVIEWER)
    assert pending['items'] == []
    _, out, _ = call(server, 'POST', '/api/v0/policies/x/validate', REVIEWER,
                     {'bad': 1})
    assert out['valid'] is False


def test_openapi_served_and_matches_key_responses(server):
    code, spec, _ = call(server, 'GET', '/api/v0/openapi.json')
    assert code == 200 and spec['openapi'].startswith('3.1')
    jsonschema = pytest.importorskip('jsonschema')

    def check(instance, ref):
        name = ref.rsplit('/', 1)[1]
        schema = {**spec['components']['schemas'][name],
                  'components': spec['components']}
        jsonschema.Draft202012Validator(schema).validate(instance)

    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, sub, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    check(sub, '#/components/schemas/SubmitResult')
    _, wi, _ = call(server, 'GET', f'/api/v0/hitl/{sub["hitl_id"]}', REVIEWER)
    check(wi, '#/components/schemas/Workitem')
    _, res, _ = call(server, 'POST', f'/api/v0/hitl/{sub["hitl_id"]}/resolve',
                     REVIEWER, {'decision': 'approve'})
    check(res, '#/components/schemas/ResolveResult')
    _, audit, _ = call(server, 'GET',
                       f'/api/v0/audit?request_id={sub["request_id"]}', REVIEWER)
    for event in audit['items']:
        check(event, '#/components/schemas/AuditEvent')
    _, err, _ = call(server, 'GET', '/api/v0/policies')
    check(err, '#/components/schemas/Error')


def test_provider_failure_falls_back_safely_with_audit():
    """OSS-005 acceptance: provider failure follows the safe Community
    fallback and produces an audit event. Exercised at the composition seam
    with a crashing provider registered."""
    from inquiryon_amp_core import InMemoryPolicyStore
    from inquiryon_amp_community import build_local_service
    from inquiryon_amp_community.identity import Principal

    class Crashing:
        name, version = 'crashing', '0'

        def decide(self, request):
            raise RuntimeError('boom')

    store = InMemoryPolicyStore()
    store.put_policy('invoice-governance', POLICY)
    service = build_local_service(
        store, {'amp_k_test_r': Principal('alice', 'reviewer')},
        callback=type('NullCb', (), {'deliver': lambda *a, **k: None})(),
        provider=Crashing())
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 40},  # policy itself would allow
                                 requested_by='agent:invoice-agent')
    assert out['status'] == 'pending_review'  # safe fallback: human review
    events = {e['event_type']: e for e in service.audit_history(out['request_id'])}
    assert events['decision.completed']['payload']['reason_code'] == 'provider_error'


def test_ui_served_with_csp_and_accessible_markup(server):
    code, html, headers = call(server, 'GET', '/', raw=True)
    assert code == 200
    assert 'Content-Security-Policy' in headers
    assert "default-src 'self'" in headers['Content-Security-Policy']
    text = html.decode()
    assert '<label' in text and 'aria-live' in text and '<nav' in text
    assert 'sk-' not in text  # no stray secrets in shipped assets
    code, js, _ = call(server, 'GET', '/ui/app.js', raw=True)
    assert code == 200
    code, out, _ = call(server, 'GET', '/ui/../server.py', raw=True)
    assert code == 404  # traversal blocked


def test_unknown_route_is_enveloped_404(server):
    code, out, _ = call(server, 'GET', '/api/v0/nope', REVIEWER)
    assert code == 404 and out['error']['code'] == 'not_found'


# -- OSS-006 contract-audit corrections --------------------------------------

def test_modify_without_modified_params_is_rejected(server):
    """Regression: decision=modify used to silently coerce a missing/null
    modified_params to {} and execute it, bypassing the intended structured
    replacement requirement (v0.json's HITLDecision if/then). Omitting the
    field, or sending it null, must now be rejected — never executed."""
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, out, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    code, res, _ = call(server, 'POST', f'/api/v0/hitl/{out["hitl_id"]}/resolve',
                        REVIEWER, {'decision': 'modify'})
    assert code == 422 and res['error']['code'] == 'validation_failed'
    # Still pending: nothing executed, no callback fired.
    _, wi, _ = call(server, 'GET', f'/api/v0/hitl/{out["hitl_id"]}', REVIEWER)
    assert wi['status'] == 'pending'

    code, res, _ = call(server, 'POST', f'/api/v0/hitl/{out["hitl_id"]}/resolve',
                        REVIEWER, {'decision': 'modify', 'modified_params': None})
    assert code == 422 and res['error']['code'] == 'validation_failed'


def test_modify_with_non_object_modified_params_is_rejected(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, out, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    code, res, _ = call(server, 'POST', f'/api/v0/hitl/{out["hitl_id"]}/resolve',
                        REVIEWER, {'decision': 'modify',
                                   'modified_params': 'not-an-object'})
    assert code == 422 and res['error']['code'] == 'validation_failed'


def test_callback_delivery_status_and_targeted_retry(server):
    call(server, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
    _, out, _ = call(server, 'POST', '/api/v0/requests', AGENT, {
        'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
        'action': 'submit', 'params': {'amount': 5000}})
    _, resolved, _ = call(server, 'POST', f'/api/v0/hitl/{out["hitl_id"]}/resolve',
                          REVIEWER, {'decision': 'approve'})
    delivery_id = resolved['delivery_id']

    code, status, _ = call(server, 'GET', f'/api/v0/callbacks/{delivery_id}', REVIEWER)
    assert code == 200
    assert status['delivery_id'] == delivery_id
    assert status['hitl_id'] == out['hitl_id']
    assert status['status'] == 'delivered'
    assert status['attempts'] >= 1

    # Idempotent: retrying an already-delivered record reports delivered
    # without incrementing attempts via a real re-send.
    code, retried, _ = call(server, 'POST',
                            f'/api/v0/callbacks/{delivery_id}/retry', REVIEWER)
    assert code == 200 and retried['delivered'] is True and retried['status'] == 'delivered'

    # An unscoped viewer may view (matches identity.can_view for reviewers/
    # viewers with no scope restriction) but may not retry (reviewer-only).
    code, out2, _ = call(server, 'GET', f'/api/v0/callbacks/{delivery_id}', VIEWER)
    assert code == 200
    code, out2b, _ = call(server, 'POST',
                          f'/api/v0/callbacks/{delivery_id}/retry', VIEWER)
    assert code == 403

    # Unknown delivery id -> 404 envelope.
    code, out3, _ = call(server, 'GET', '/api/v0/callbacks/dlv-does-not-exist',
                         REVIEWER)
    assert code == 404 and out3['error']['code'] == 'not_found'
    code, out3b, _ = call(server, 'POST',
                          '/api/v0/callbacks/dlv-does-not-exist/retry', REVIEWER)
    assert code == 404 and out3b['error']['code'] == 'not_found'

    # Only reviewers may retry (the requesting agent may not, even though it
    # can view its own request's delivery).
    code, out4, _ = call(server, 'POST',
                         f'/api/v0/callbacks/{delivery_id}/retry', AGENT)
    assert code == 403


def test_callback_delivery_status_denied_across_scope():
    """A reviewer scoped to a different opaque scope than the workitem may
    not view its delivery status — exercises the new authorization branch
    directly (the shared `server` fixture has no scoped principals)."""
    tmp = tempfile.mkdtemp(prefix='amp-community-test-')
    config = {
        'db_path': os.path.join(tmp, 'amp.db'),
        'policy_dir': os.path.join(tmp, 'policies'),
        'callback': {'mode': 'poll'},
        'tokens': [
            {'token': REVIEWER, 'principal': 'alice', 'role': 'reviewer'},
            {'token': AGENT, 'principal': 'agent:invoice-agent', 'role': 'service',
             'scope': 'team-a'},
            {'token': 'amp_k_test_other_scope', 'principal': 'bob',
             'role': 'reviewer', 'scope': 'team-b'},
        ],
    }
    ctx = build_api_context(config)
    httpd, port = start_background(ctx)
    base = f'http://127.0.0.1:{port}'
    try:
        call(base, 'PUT', '/api/v0/policies/invoice-governance', REVIEWER, POLICY)
        _, out, _ = call(base, 'POST', '/api/v0/requests', AGENT, {
            'policy_id': 'invoice-governance', 'tool': 'pay_invoice',
            'action': 'submit', 'params': {'amount': 5000}})
        _, resolved, _ = call(base, 'POST', f'/api/v0/hitl/{out["hitl_id"]}/resolve',
                              REVIEWER, {'decision': 'approve'})
        delivery_id = resolved['delivery_id']
        code, err, _ = call(base, 'GET', f'/api/v0/callbacks/{delivery_id}',
                            'amp_k_test_other_scope')
        assert code == 403 and err['error']['code'] == 'forbidden'
    finally:
        httpd.shutdown()


def test_orphaned_callback_delivery_fails_closed():
    """A delivery whose owning workitem is unavailable cannot be scoped or
    authorized, so its metadata must not be exposed."""
    tmp = tempfile.mkdtemp(prefix='amp-community-test-')
    ctx = build_api_context({
        'db_path': os.path.join(tmp, 'amp.db'),
        'policy_dir': os.path.join(tmp, 'policies'),
        'callback': {'mode': 'poll'},
        'tokens': [
            {'token': REVIEWER, 'principal': 'alice', 'role': 'reviewer'},
        ],
    })
    service = ctx.service
    service.dispatcher.deliveries.create(
        'dlv-orphan', 'req-missing', 'hitl-missing', {'decision': 'approved'},
        service.hitl.clock.now_iso())
    with pytest.raises(NotFoundError):
        service.get_delivery('alice', 'dlv-orphan')


def test_policy_with_unsupported_schema_version_is_rejected(server):
    """Regression: schema_version is a `const: "v0"` field in v0.json; the
    runtime previously accepted any value because jsonschema was only an
    optional dependency and the no-jsonschema fallback checks `required`
    fields only, never `const`/`enum`. jsonschema is now a hard dependency
    of amp-community specifically so this is enforced."""
    bad = {**POLICY, 'policy_id': 'bad-version', 'schema_version': 'v1'}
    code, out, _ = call(server, 'PUT', '/api/v0/policies/bad-version',
                        REVIEWER, bad)
    assert code == 422
    assert out['error']['code'] == 'invalid_policy'
