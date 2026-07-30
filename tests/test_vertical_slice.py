"""End-to-end vertical slice (OSS-004 acceptance / Phase 1 exit test):

tool request -> deterministic evaluation -> allow or human review ->
authorized approve/reject/modify (validated + re-evaluated) -> callback
delivery with retry state -> complete append-only audit history.

Runs entirely on Community components: amp-core + SQLite + local identity +
a fake in-process callback. No Enterprise code, no network, no MySQL.
"""

import itertools

import pytest

from amp_core import InMemoryPolicyStore
from amp_community import (
    AuthorizationError,
    ConflictError,
    build_local_service,
)
from amp_community.identity import Principal

POLICY = {
    'policy_id': 'invoice-governance',
    'allow_unknown_tools': False,
    'action_governance': {'criteria': [
        {'criterion_id': 'c-high-amount', 'tool': 'pay_invoice', 'action': '*',
         'hardness': 'hard',
         'evaluator': {'type': 'compute', 'formula': 'amount > 1000'}},
        {'criterion_id': 'c-foreign-currency', 'tool': 'pay_invoice', 'action': '*',
         'hardness': 'soft',
         'evaluator': {'type': 'compute', 'formula': "get('currency', 'USD') != 'USD'"}},
    ]},
}

TOKENS = {
    'amp_k_test_reviewer': Principal('alice', 'reviewer'),
    'amp_k_test_viewer': Principal('victor', 'viewer'),
    'amp_k_test_agent': Principal('agent:invoice-agent', 'service'),
}


class FakeClock:
    def __init__(self):
        self._t = itertools.count()

    def now_iso(self):
        return f'2026-07-22T10:00:{next(self._t):02d}Z'

    def monotonic(self):
        return float(next(self._t))


class FakeIds:
    def __init__(self):
        self._n = itertools.count(1)

    def new_id(self, kind):
        return f'{kind}-{next(self._n):04d}'


class FakeCallback:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.deliveries = []

    def deliver(self, delivery_id, request_id, result):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError('simulated network failure')
        self.deliveries.append((delivery_id, request_id, result))


def make_service(callback=None):
    store = InMemoryPolicyStore()
    store.put_policy('invoice-governance', POLICY)
    callback = callback or FakeCallback()
    service = build_local_service(
        store, TOKENS, callback, clock=FakeClock(), ids=FakeIds(),
        policy_resolver=lambda wi: POLICY)
    return service, callback


def test_allowed_request_never_touches_hitl():
    service, _cb = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 40}, requested_by='agent:invoice-agent')
    assert out['status'] == 'allowed'
    events = [e['event_type'] for e in service.audit_history(out['request_id'])]
    assert events == ['evaluation.completed', 'decision.completed']


def test_full_slice_approve_path():
    service, callback = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 5000}, requested_by='agent:invoice-agent')
    assert out['status'] == 'pending_review'
    hitl_id = out['hitl_id']

    pending = service.hitl.list_pending('alice')
    assert [w['hitl_id'] for w in pending] == [hitl_id]

    resolved = service.resolve('alice', hitl_id, 'approve', note='verified vendor')
    assert resolved['delivered'] is True
    assert callback.deliveries[0][2] == {'decision': 'approved',
                                         'params': {'amount': 5000}}
    assert service.hitl.store.get(hitl_id)['status'] == 'resumed'

    events = [e['event_type'] for e in service.audit_history(out['request_id'])]
    assert events == ['evaluation.completed', 'decision.completed',
                      'hitl.requested', 'hitl.pending', 'hitl.approved',
                      'callback.delivered', 'hitl.resumed']


def test_reject_path_closes_item():
    service, callback = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 9999}, requested_by='agent:invoice-agent')
    resolved = service.resolve('alice', out['hitl_id'], 'reject', note='fraud risk')
    assert resolved['result']['params'] is None
    assert service.hitl.store.get(out['hitl_id'])['status'] == 'closed'


def test_modify_reevaluates_and_executes_when_policy_allows():
    service, callback = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 5000}, requested_by='agent:invoice-agent')
    resolved = service.resolve('alice', out['hitl_id'], 'modify',
                               modified_params={'amount': 900})
    assert resolved['result']['executed'] is True
    assert resolved['result']['params'] == {'amount': 900}
    assert resolved['result']['follow_up_hitl_id'] is None
    # Original and final actions are in the audit trail.
    modified_events = service.audit.query(request_id=out['request_id'],
                                          event_type='hitl.modified')
    assert modified_events[0]['payload']['original_params'] == {'amount': 5000}
    assert modified_events[0]['payload']['modified_params'] == {'amount': 900}


def test_modify_failing_hard_policy_never_executes():
    service, callback = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 5000}, requested_by='agent:invoice-agent')
    resolved = service.resolve('alice', out['hitl_id'], 'modify',
                               modified_params={'amount': 99999})
    assert resolved['result']['executed'] is False
    assert resolved['result']['params'] is None
    follow_up = resolved['result']['follow_up_hitl_id']
    assert follow_up is not None
    assert service.hitl.store.get(follow_up)['status'] == 'pending'
    assert callback.deliveries[0][2]['params'] is None  # agent told: not executed


def test_callback_failure_visible_and_retryable_without_duplicate_decisions():
    callback = FakeCallback(fail_times=2)
    service, _ = make_service(callback)
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 5000}, requested_by='agent:invoice-agent')
    resolved = service.resolve('alice', out['hitl_id'], 'approve')
    assert resolved['delivered'] is False
    record = service.dispatcher.deliveries.get(resolved['delivery_id'])
    assert record['status'] == 'failed' and record['attempts'] == 1

    assert service.retry_callbacks()[resolved['delivery_id']] is False  # fails again
    assert service.retry_callbacks()[resolved['delivery_id']] is True   # succeeds
    # Extra retries are idempotent: no re-send after delivery.
    service.retry_callbacks()
    assert len(callback.deliveries) == 1
    assert service.hitl.store.get(out['hitl_id'])['status'] == 'resumed'


def test_idempotent_submit_and_resolve():
    service, callback = make_service()
    out1 = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                  {'amount': 5000}, requested_by='agent:invoice-agent',
                                  request_id='req-fixed')
    out2 = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                  {'amount': 5000}, requested_by='agent:invoice-agent',
                                  request_id='req-fixed')
    assert out2['replayed'] is True and out2['hitl_id'] == out1['hitl_id']

    service.resolve('alice', out1['hitl_id'], 'approve')
    replay = service.resolve('alice', out1['hitl_id'], 'approve')
    assert replay['replayed'] is True
    assert len(callback.deliveries) == 1  # no duplicate delivery record content
    with pytest.raises(ConflictError):
        service.resolve('alice', out1['hitl_id'], 'reject')


def test_authorization_rules():
    service, _cb = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 5000}, requested_by='agent:invoice-agent')
    hitl_id = out['hitl_id']
    with pytest.raises(AuthorizationError):
        service.resolve('victor', hitl_id, 'approve')  # viewer cannot decide
    with pytest.raises(AuthorizationError):
        service.resolve('agent:invoice-agent', hitl_id, 'approve')  # own request
    assert service.hitl.get('victor', hitl_id)['hitl_id'] == hitl_id  # can view


def test_unknown_tool_routes_to_review():
    service, _cb = make_service()
    out = service.submit_request('invoice-governance', 'delete_database', 'run',
                                 {}, requested_by='agent:invoice-agent')
    assert out['status'] == 'pending_review'
    assert service.hitl.store.get(out['hitl_id'])['reason'] == 'unknown_tool_not_allowed'


def test_audit_is_append_only_and_queryable():
    service, _cb = make_service()
    out = service.submit_request('invoice-governance', 'pay_invoice', 'submit',
                                 {'amount': 5000}, requested_by='agent:invoice-agent')
    service.resolve('alice', out['hitl_id'], 'approve')
    events = service.audit_history(out['request_id'])
    assert len(events) == 7
    # No public mutation API exists; the storage class exposes only append,
    # query, and the sanctioned retention purge.
    sink = service.audit
    assert not hasattr(sink, 'update') and not hasattr(sink, 'delete')
    by_type = service.audit.query(request_id=out['request_id'],
                                  event_type='hitl.approved')
    assert len(by_type) == 1 and by_type[0]['actor'] == 'user:alice'
