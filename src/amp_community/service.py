"""Community governance service — the first-release vertical slice (OSS-004).

    tool request
      -> deterministic policy evaluation (amp-core)
      -> decision provider under safety invariants
      -> allow, or human-review routing
      -> authorized approve / reject / modify (with re-evaluation)
      -> callback delivery with persisted retry state
      -> complete append-only audit history

REST-neutral: OSS-005 exposes these methods over HTTP. Composition happens
at the deployment edge (see ``build_local_service``); Enterprise registers
its own provider/adapters at its private composition root instead.
"""

from __future__ import annotations

from typing import Optional

from amp_core import (
    AuditEvent,
    DecisionRequest,
    StaticPolicyProvider,
    decide_with_safety,
    run_eval_policy,
)
from amp_core.interfaces import SystemClock, UuidGenerator
from amp_core.schema import validate

from .callbacks import CallbackDispatcher
from .hitl import AuthorizationError, HitlService, NotFoundError, ValidationError
from .identity import LocalIdentityProvider, Principal
from .storage import (
    SqliteAuditSink,
    SqliteBackend,
    SqliteDeliveryStore,
    SqliteWorkItemStore,
)


class GovernanceService:
    def __init__(self, policy_store, hitl: HitlService,
                 dispatcher: CallbackDispatcher, audit_sink, clock, ids,
                 provider=None):
        self.policies = policy_store
        self.hitl = hitl
        self.dispatcher = dispatcher
        self.audit = audit_sink
        self.clock = clock
        self.ids = ids
        self.provider = provider or StaticPolicyProvider()

    def _emit(self, event_type: str, request_id: str, payload: dict,
              scope: Optional[str] = None, actor: str = 'system'):
        self.audit.append(AuditEvent(
            event_id=self.ids.new_id('evt'), event_type=event_type,
            occurred_at=self.clock.now_iso(), request_id=request_id,
            actor=actor, payload=payload, scope=scope))

    # -- intake -----------------------------------------------------------
    def submit_request(self, policy_id: str, tool: str, action: str,
                       params: dict, requested_by: str,
                       request_id: Optional[str] = None,
                       criteria_inputs: Optional[dict] = None,
                       scope: Optional[str] = None) -> dict:
        """Evaluate a tool request; allow immediately or open a review.

        Idempotent per ``request_id``: resubmission returns the current state
        instead of re-deciding.
        """
        request_id = request_id or self.ids.new_id('req')
        existing = self.hitl.store.find_by_request_id(request_id)
        if existing is not None:
            return {'request_id': request_id, 'status': 'pending_review',
                    'hitl_id': existing['hitl_id'], 'replayed': True}

        request_doc = {'request_id': request_id, 'tool': tool, 'action': action,
                       'params': dict(params or {})}
        errors = validate(request_doc, 'EvaluationRequest')
        if errors:
            raise ValidationError(f'invalid evaluation request: {errors}')

        policy = self.policies.get_policy(policy_id)
        if policy is None:
            raise ValidationError(f'unknown policy: {policy_id}')

        evaluation = run_eval_policy(policy, tool, action, params,
                                     criteria_inputs=criteria_inputs)
        self._emit('evaluation.completed', request_id, {
            'policy_id': policy_id, 'tool': tool, 'action': action,
            'decision': evaluation['decision'], 'reason': evaluation['reason'],
            'triggered_criteria': [c.get('criterion_id')
                                   for c in evaluation.get('triggered_criteria', [])],
        }, scope)

        decision = decide_with_safety(self.provider, DecisionRequest(
            request_id=request_id, tool=tool, action=action,
            params=dict(params or {}), policy_evaluation=evaluation, scope=scope))
        self._emit('decision.completed', request_id, {
            'outcome': decision.outcome, 'reason_code': decision.reason_code,
            'provider': {'name': decision.provider_name,
                         'version': decision.provider_version},
        }, scope, actor=f'provider:{decision.provider_name}')

        if decision.outcome == 'allow':
            return {'request_id': request_id, 'status': 'allowed',
                    'reason': decision.reason_code}
        if decision.outcome == 'deny':
            return {'request_id': request_id, 'status': 'denied',
                    'reason': decision.reason_code}

        workitem = self.hitl.create_request(
            request_id=request_id, tool=tool, action=action, params=params,
            reason=decision.reason_code,
            triggered_criteria=[c.get('criterion_id')
                                for c in evaluation.get('triggered_criteria', [])],
            requested_by=requested_by, scope=scope, policy_id=policy_id)
        return {'request_id': request_id, 'status': 'pending_review',
                'hitl_id': workitem['hitl_id']}

    # -- resolution -------------------------------------------------------
    def resolve(self, principal: str, hitl_id: str, decision: str,
                modified_params: Optional[dict] = None, note: str = '') -> dict:
        """Approve/reject/modify, then enqueue+attempt callback delivery."""
        if decision == 'approve':
            outcome = self.hitl.approve(principal, hitl_id, note)
            result = {'decision': 'approved', 'params': outcome.executed_params}
        elif decision == 'reject':
            outcome = self.hitl.reject(principal, hitl_id, note)
            result = {'decision': 'rejected', 'params': None}
        elif decision == 'modify':
            # Do not silently coerce a missing/null modified_params to {}:
            # HitlService.modify() is the single point that enforces the
            # structured-object requirement (OSS-006 compatibility fix —
            # previously an omitted field defaulted here and executed with
            # empty params, bypassing the intended re-review).
            outcome = self.hitl.modify(principal, hitl_id, modified_params, note)
            result = {'decision': 'modified',
                      'params': outcome.executed_params,
                      'executed': outcome.executed_params is not None,
                      'follow_up_hitl_id': outcome.follow_up_hitl_id}
        else:
            raise ValidationError(f'unknown decision {decision!r}')

        workitem = self.hitl.store.get(hitl_id)
        delivery_id = self.dispatcher.enqueue(workitem['request_id'], hitl_id, result)
        delivered = self.dispatcher.attempt(delivery_id)
        if delivered:
            self.hitl.close(hitl_id, 'resumed' if result.get('params') is not None
                            else 'closed')
        return {'hitl_id': hitl_id, 'result': result,
                'delivery_id': delivery_id, 'delivered': delivered,
                'replayed': outcome.replayed}

    def retry_callbacks(self) -> dict:
        """Retry undelivered callbacks and finalize items on success."""
        results = self.dispatcher.retry_undelivered()
        for delivery_id, ok in results.items():
            if ok:
                record = self.dispatcher.deliveries.get(delivery_id)
                payload = record['payload']
                self.hitl.close(record['hitl_id'],
                                'resumed' if payload.get('params') is not None
                                else 'closed')
        return results

    # -- per-delivery status/retry (OSS-006 compatibility addition) -------
    def _delivery_for_principal(self, principal: str, delivery_id: str) -> dict:
        """Fetch a delivery record, enforcing the same visibility rule as
        the underlying workitem (whoever may view the review may see its
        delivery status)."""
        record = self.dispatcher.deliveries.get(delivery_id)
        if record is None:
            raise NotFoundError(delivery_id)
        workitem = self.hitl.store.get(record['hitl_id'])
        # A delivery without its owning workitem cannot be authorized safely.
        # Treat it as unavailable instead of exposing orphaned delivery
        # metadata to any authenticated caller that can guess its id.
        if workitem is None:
            raise NotFoundError(delivery_id)
        if not self.hitl.identity.can_view(principal, workitem):
            raise AuthorizationError(f'{principal} may not view delivery {delivery_id}')
        return record

    def get_delivery(self, principal: str, delivery_id: str) -> dict:
        """Status of one callback delivery (new: previously only a bulk
        retry-everything endpoint existed, with no way to check a single
        delivery or target a retry at it)."""
        record = self._delivery_for_principal(principal, delivery_id)
        return {'delivery_id': record['delivery_id'], 'hitl_id': record['hitl_id'],
                'request_id': record['request_id'], 'status': record['status'],
                'attempts': record['attempts'], 'last_error': record['last_error'],
                'created_at': record['created_at'], 'updated_at': record['updated_at']}

    def retry_delivery(self, principal: str, delivery_id: str) -> dict:
        """Retry exactly one delivery. Idempotent: an already-delivered
        record is reported delivered without re-sending."""
        self._delivery_for_principal(principal, delivery_id)
        delivered = self.dispatcher.attempt(delivery_id)
        if delivered:
            record = self.dispatcher.deliveries.get(delivery_id)
            payload = record['payload']
            self.hitl.close(record['hitl_id'],
                            'resumed' if payload.get('params') is not None
                            else 'closed')
        return {'delivery_id': delivery_id, 'delivered': delivered,
                'status': self.dispatcher.deliveries.get(delivery_id)['status']}

    # -- history ----------------------------------------------------------
    def audit_history(self, request_id: str) -> list:
        return self.audit.query(request_id=request_id)


def build_local_service(policy_store, tokens: dict, callback,
                        db_path: str = ':memory:', clock=None, ids=None,
                        provider=None, policy_resolver=None) -> GovernanceService:
    """Community composition root: SQLite + local identity + one callback."""
    clock = clock or SystemClock()
    ids = ids or UuidGenerator()
    backend = SqliteBackend(db_path)
    audit_sink = SqliteAuditSink(backend)
    identity = LocalIdentityProvider(tokens)
    if policy_resolver is None:
        # Default: re-evaluate modifications against the policy that opened
        # the review (stored on the workitem); no policy => mandatory re-review.
        def policy_resolver(workitem):
            pid = workitem.get('policy_id')
            return policy_store.get_policy(pid) if pid else None
    hitl = HitlService(SqliteWorkItemStore(backend), identity, audit_sink,
                       clock, ids, policy_resolver=policy_resolver)
    dispatcher = CallbackDispatcher(SqliteDeliveryStore(backend), callback,
                                    audit_sink, clock, ids)
    return GovernanceService(policy_store, hitl, dispatcher, audit_sink,
                             clock, ids, provider=provider)


__all__ = ['GovernanceService', 'build_local_service', 'Principal']
