"""Community HITL state machine and application service (OSS-004).

State model (v0):

    requested -> pending -> approved | rejected | modified -> resumed | closed
                 pending -> expired | cancelled

- ``requested`` is transient: creation atomically stores the workitem as
  ``pending`` and emits both ``hitl.requested`` and ``hitl.pending`` audit
  events, so the persisted set of states is
  ``pending/approved/rejected/modified/expired/cancelled/resumed/closed``.
- Creation is idempotent by ``request_id``; resolution is idempotent by
  (hitl_id, decision): repeating the same decision returns the stored
  outcome, a conflicting decision raises :class:`ConflictError`.
- Completed decisions cannot be altered through this service (append-only
  audit plus terminal-state checks); there is no update path to a decided
  item's decision fields.
- ``modify`` accepts a **structured replacement params object** (free-form
  notes are never treated as the modified action), schema-validates it,
  re-evaluates it against policy via amp-core, and never lets a
  hard-policy failure execute: a failing modification spawns a linked
  follow-up review instead of resuming the agent.

Authorization is separated from authentication: an
:class:`amp_core.interfaces.IdentityProvider` answers view/decide questions;
requesters may never decide their own requests; service identities
(``provider:*``/``agent:*``) may create but not decide. Scope is opaque —
items are visible only to principals whose scope matches (or reviewers with
no scope restriction). Host applications may layer additional checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from amp_core import AuditEvent, run_eval_policy
from amp_core.interfaces import AuditEventSink, Clock, IdGenerator
from amp_core.schema import validate

PENDING = 'pending'
APPROVED = 'approved'
REJECTED = 'rejected'
MODIFIED = 'modified'
EXPIRED = 'expired'
CANCELLED = 'cancelled'
RESUMED = 'resumed'
CLOSED = 'closed'

DECISION_STATES = {APPROVED, REJECTED, MODIFIED}
TERMINAL_STATES = {RESUMED, CLOSED, EXPIRED, CANCELLED}

LEGAL_TRANSITIONS = {
    PENDING: {APPROVED, REJECTED, MODIFIED, EXPIRED, CANCELLED},
    APPROVED: {RESUMED, CLOSED},
    REJECTED: {RESUMED, CLOSED},
    MODIFIED: {RESUMED, CLOSED},
    EXPIRED: set(),
    CANCELLED: set(),
    RESUMED: set(),
    CLOSED: set(),
}


class HitlError(Exception):
    pass


class NotFoundError(HitlError):
    pass


class ConflictError(HitlError):
    """Illegal transition or conflicting idempotent replay."""


class AuthorizationError(HitlError):
    pass


class ValidationError(HitlError):
    pass


@dataclass
class HitlDecisionOutcome:
    hitl_id: str
    status: str                  # approved | rejected | modified
    executed_params: Optional[dict]  # params the agent may execute; None if blocked
    follow_up_hitl_id: Optional[str] = None  # set when a modification re-review spawned
    replayed: bool = False       # True when an idempotent replay returned stored state


class HitlService:
    """REST-neutral human-decision workflow over injected adapters."""

    def __init__(self, workitem_store, identity_provider, audit_sink: AuditEventSink,
                 clock: Clock, id_generator: IdGenerator,
                 policy_resolver=None):
        """``policy_resolver(workitem) -> policy dict`` enables modification
        re-evaluation; without it, all modifications require re-review."""
        self.store = workitem_store
        self.identity = identity_provider
        self.audit = audit_sink
        self.clock = clock
        self.ids = id_generator
        self.policy_resolver = policy_resolver

    # -- audit ------------------------------------------------------------
    def _emit(self, event_type: str, request_id: str, actor: str,
              payload: dict, scope: Optional[str]):
        self.audit.append(AuditEvent(
            event_id=self.ids.new_id('evt'),
            event_type=event_type,
            occurred_at=self.clock.now_iso(),
            request_id=request_id,
            actor=actor,
            payload=payload,
            scope=scope,
        ))

    # -- creation ---------------------------------------------------------
    def create_request(self, request_id: str, tool: str, action: str,
                       params: dict, reason: str,
                       triggered_criteria: Optional[list] = None,
                       requested_by: str = 'system',
                       scope: Optional[str] = None,
                       expires_at: Optional[str] = None,
                       policy_id: Optional[str] = None) -> dict:
        """Create (or idempotently return) the review for ``request_id``."""
        existing = self.store.find_by_request_id(request_id)
        if existing is not None:
            return existing

        hitl_id = self.ids.new_id('hitl')
        workitem = {
            'hitl_id': hitl_id,
            'request_id': request_id,
            'tool': tool,
            'action': action,
            'params': dict(params or {}),
            'reason': reason,
            'triggered_criteria': list(triggered_criteria or []),
            'status': PENDING,
            'requested_by': requested_by,
            'scope': scope,
            'created_at': self.clock.now_iso(),
            'expires_at': expires_at,
            'policy_id': policy_id,
            'decision': None,
        }
        errors = validate(workitem, 'HITLRequest')
        if errors:
            raise ValidationError(f'invalid HITL request: {errors}')
        self.store.create(workitem)
        self._emit('hitl.requested', request_id, requested_by,
                   {'hitl_id': hitl_id, 'tool': tool, 'action': action,
                    'reason': reason}, scope)
        self._emit('hitl.pending', request_id, 'system', {'hitl_id': hitl_id}, scope)
        return workitem

    # -- visibility -------------------------------------------------------
    def _check_view(self, principal: str, workitem: dict):
        if not self.identity.can_view(principal, workitem):
            raise AuthorizationError(f'{principal} may not view {workitem["hitl_id"]}')

    def get(self, principal: str, hitl_id: str) -> dict:
        workitem = self.store.get(hitl_id)
        if workitem is None:
            raise NotFoundError(hitl_id)
        self._check_view(principal, workitem)
        return workitem

    def list_pending(self, principal: str) -> list:
        return [w for w in self.store.list_by_status(PENDING)
                if self.identity.can_view(principal, w)]

    # -- decisions --------------------------------------------------------
    def _load_for_decision(self, principal: str, hitl_id: str, decision: str):
        workitem = self.store.get(hitl_id)
        if workitem is None:
            raise NotFoundError(hitl_id)
        self._check_view(principal, workitem)
        if workitem['status'] in DECISION_STATES or workitem['status'] in TERMINAL_STATES:
            stored = (workitem.get('decision') or {})
            if stored.get('decision') == decision:
                return workitem, True  # idempotent replay
            raise ConflictError(
                f'{hitl_id} already {workitem["status"]}; cannot {decision}')
        if decision not in LEGAL_TRANSITIONS.get(workitem['status'], set()) \
                and workitem['status'] != PENDING:
            raise ConflictError(f'illegal transition {workitem["status"]} -> {decision}')
        if not self.identity.can_decide(principal, workitem):
            raise AuthorizationError(f'{principal} may not decide {hitl_id}')
        if principal == workitem.get('requested_by'):
            raise AuthorizationError('requesters may not review their own actions')
        return workitem, False

    def _record_decision(self, workitem: dict, status: str, principal: str,
                         note: str, extra: dict):
        decision_record = {
            'decision': status,
            'decided_by': principal,
            'decided_at': self.clock.now_iso(),
            'note': note or '',
            **extra,
        }
        self.store.transition(workitem['hitl_id'], PENDING, status, decision_record)
        self._emit(f'hitl.{status}', workitem['request_id'], f'user:{principal}',
                   {'hitl_id': workitem['hitl_id'], 'note': note or '', **extra},
                   workitem.get('scope'))
        return decision_record

    def approve(self, principal: str, hitl_id: str, note: str = '') -> HitlDecisionOutcome:
        workitem, replay = self._load_for_decision(principal, hitl_id, APPROVED)
        if replay:
            return HitlDecisionOutcome(hitl_id, APPROVED, workitem['params'], replayed=True)
        self._record_decision(workitem, APPROVED, principal, note, {})
        return HitlDecisionOutcome(hitl_id, APPROVED, workitem['params'])

    def reject(self, principal: str, hitl_id: str, note: str = '') -> HitlDecisionOutcome:
        workitem, replay = self._load_for_decision(principal, hitl_id, REJECTED)
        if replay:
            return HitlDecisionOutcome(hitl_id, REJECTED, None, replayed=True)
        self._record_decision(workitem, REJECTED, principal, note, {})
        return HitlDecisionOutcome(hitl_id, REJECTED, None)

    def modify(self, principal: str, hitl_id: str, modified_params: dict,
               note: str = '') -> HitlDecisionOutcome:
        """Safe modification flow (OSS-004): validate, record, re-evaluate,
        never execute a hard-policy failure, re-review when required."""
        if modified_params is None or not isinstance(modified_params, dict):
            raise ValidationError(
                'modified_params is required and must be a structured object '
                'when decision=modify (free-form notes are never treated as '
                'the modified action)')
        workitem, replay = self._load_for_decision(principal, hitl_id, MODIFIED)
        if replay:
            stored = workitem.get('decision') or {}
            return HitlDecisionOutcome(
                hitl_id, MODIFIED,
                stored.get('executed_params'),
                follow_up_hitl_id=stored.get('follow_up_hitl_id'),
                replayed=True)

        candidate = {
            'request_id': workitem['request_id'],
            'tool': workitem['tool'],
            'action': workitem['action'],
            'params': modified_params,
        }
        errors = validate(candidate, 'EvaluationRequest')
        if errors:
            raise ValidationError(f'modified action failed schema validation: {errors}')

        # Re-evaluate against policy. No resolvable policy => mandatory re-review.
        policy = self.policy_resolver(workitem) if self.policy_resolver else None
        if policy is not None:
            evaluation = run_eval_policy(policy, workitem['tool'], workitem['action'],
                                         modified_params)
        else:
            evaluation = {'decision': 'require_human',
                          'reason': 'no_policy_resolver_re_review_required'}

        follow_up_id = None
        executed = None
        if evaluation['decision'] == 'allow':
            executed = modified_params
        else:
            # Hard failure or mandated review: never execute; spawn follow-up.
            follow_up = self.create_request(
                request_id=self.ids.new_id('req'),
                tool=workitem['tool'], action=workitem['action'],
                params=modified_params,
                reason=f're_review_of_modification:{evaluation["reason"]}',
                triggered_criteria=[
                    c.get('criterion_id') for c in evaluation.get('triggered_criteria', [])
                    if isinstance(c, dict)],
                requested_by=f'user:{principal}',
                scope=workitem.get('scope'),
                policy_id=workitem.get('policy_id'))
            follow_up_id = follow_up['hitl_id']

        self._record_decision(workitem, MODIFIED, principal, note, {
            'original_params': workitem['params'],
            'modified_params': modified_params,
            'reevaluation': {'decision': evaluation['decision'],
                             'reason': evaluation['reason']},
            'executed_params': executed,
            'follow_up_hitl_id': follow_up_id,
        })
        return HitlDecisionOutcome(hitl_id, MODIFIED, executed,
                                   follow_up_hitl_id=follow_up_id)

    # -- lifecycle end ----------------------------------------------------
    def close(self, hitl_id: str, outcome: str = CLOSED, actor: str = 'system'):
        """Move a decided item to resumed/closed after callback delivery."""
        if outcome not in (RESUMED, CLOSED):
            raise ConflictError(f'invalid final state {outcome}')
        workitem = self.store.get(hitl_id)
        if workitem is None:
            raise NotFoundError(hitl_id)
        if workitem['status'] in (RESUMED, CLOSED):
            return workitem  # idempotent
        if outcome not in LEGAL_TRANSITIONS.get(workitem['status'], set()):
            raise ConflictError(f'illegal transition {workitem["status"]} -> {outcome}')
        self.store.transition(hitl_id, workitem['status'], outcome, None)
        self._emit(f'hitl.{outcome}', workitem['request_id'], actor,
                   {'hitl_id': hitl_id}, workitem.get('scope'))
        return self.store.get(hitl_id)

    def expire_or_cancel(self, hitl_id: str, outcome: str, actor: str = 'system'):
        if outcome not in (EXPIRED, CANCELLED):
            raise ConflictError(f'invalid outcome {outcome}')
        workitem = self.store.get(hitl_id)
        if workitem is None:
            raise NotFoundError(hitl_id)
        if workitem['status'] == outcome:
            return workitem  # idempotent
        if outcome not in LEGAL_TRANSITIONS.get(workitem['status'], set()):
            raise ConflictError(f'illegal transition {workitem["status"]} -> {outcome}')
        self.store.transition(hitl_id, PENDING, outcome, None)
        self._emit(f'hitl.{outcome}', workitem['request_id'], actor,
                   {'hitl_id': hitl_id}, workitem.get('scope'))
        return self.store.get(hitl_id)
