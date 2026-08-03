"""Retryable, idempotent agent-callback delivery (OSS-004).

New public behavior relative to the legacy best-effort single attempt:
every decision produces a persisted delivery record with a stable
``delivery_id``; failures are visible and retryable; retries are idempotent
(the receiver can deduplicate on ``delivery_id``, and a delivered record is
never re-sent). Audit events record each attempt outcome.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from inquiryon_amp_core import AuditEvent
from inquiryon_amp_core.interfaces import AuditEventSink, Clock, IdGenerator


class HttpAgentCallback:
    """Reference AgentCallback: POST JSON to a fixed URL (stdlib only)."""

    def __init__(self, url: str, timeout_seconds: float = 10.0):
        self.url = url
        self.timeout = timeout_seconds

    def deliver(self, delivery_id: str, request_id: str, result: dict) -> None:
        body = json.dumps({'delivery_id': delivery_id, 'request_id': request_id,
                           'result': result}).encode('utf-8')
        req = urllib.request.Request(
            self.url, data=body, method='POST',
            headers={'Content-Type': 'application/json',
                     'X-AMP-Delivery-Id': delivery_id})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            if resp.status >= 300:
                raise RuntimeError(f'callback returned HTTP {resp.status}')


class CallbackDispatcher:
    def __init__(self, delivery_store, callback, audit_sink: AuditEventSink,
                 clock: Clock, id_generator: IdGenerator):
        self.deliveries = delivery_store
        self.callback = callback
        self.audit = audit_sink
        self.clock = clock
        self.ids = id_generator

    def _emit(self, event_type: str, request_id: str, payload: dict):
        self.audit.append(AuditEvent(
            event_id=self.ids.new_id('evt'), event_type=event_type,
            occurred_at=self.clock.now_iso(), request_id=request_id,
            actor='system', payload=payload))

    def enqueue(self, request_id: str, hitl_id: str, result: dict) -> str:
        """Create the persisted delivery record (idempotent per delivery id)."""
        delivery_id = f'dlv-{request_id}-{hitl_id}'
        self.deliveries.create(delivery_id, request_id, hitl_id, result,
                               self.clock.now_iso())
        return delivery_id

    def attempt(self, delivery_id: str) -> bool:
        """Try one delivery. Returns True when delivered. Never raises.

        Already-delivered records return True without re-sending (idempotent
        retry). Failures persist attempt count and error for later retry.
        """
        record = self.deliveries.get(delivery_id)
        if record is None:
            return False
        if record['status'] == 'delivered':
            return True
        try:
            self.callback.deliver(delivery_id, record['request_id'], record['payload'])
        except Exception as exc:
            self.deliveries.mark(delivery_id, 'failed', self.clock.now_iso(), str(exc))
            self._emit('callback.failed', record['request_id'],
                       {'delivery_id': delivery_id, 'error': str(exc),
                        'attempts': record['attempts'] + 1})
            return False
        self.deliveries.mark(delivery_id, 'delivered', self.clock.now_iso())
        self._emit('callback.delivered', record['request_id'],
                   {'delivery_id': delivery_id})
        return True

    def retry_undelivered(self) -> dict:
        """Retry everything pending/failed; returns {delivery_id: bool}."""
        return {d: self.attempt(d) for d in self.deliveries.list_undelivered()}
