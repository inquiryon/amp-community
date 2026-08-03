"""SQLite reference storage for Community workitems, audit events, and
callback deliveries (OSS-004).

Zero external dependencies (stdlib ``sqlite3``). Postgres or other stores
implement the same small interfaces. The audit table is append-only at the
application level: this module exposes INSERT and SELECT only — no UPDATE or
DELETE path exists for audit rows except the explicit retention purge.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from inquiryon_amp_core.interfaces import AuditEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workitems (
    hitl_id     TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL,
    scope       TEXT,
    created_at  TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workitems_status ON workitems(status);

CREATE TABLE IF NOT EXISTS audit_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    scope        TEXT,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_events(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(occurred_at);

CREATE TABLE IF NOT EXISTS callback_deliveries (
    delivery_id  TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL,
    hitl_id      TEXT NOT NULL,
    status       TEXT NOT NULL,           -- pending | delivered | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delivery_status ON callback_deliveries(status);
"""


class SqliteBackend:
    def __init__(self, path: str = ':memory:'):
        # check_same_thread=False + a lock: adequate for the reference impl.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, args: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur


class SqliteWorkItemStore:
    """WorkItemStore over SQLite; body stored as canonical JSON."""

    def __init__(self, backend: SqliteBackend):
        self.db = backend

    def create(self, workitem: dict) -> str:
        self.db.execute(
            'INSERT INTO workitems (hitl_id, request_id, status, scope, created_at, body) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (workitem['hitl_id'], workitem['request_id'], workitem['status'],
             workitem.get('scope'), workitem['created_at'],
             json.dumps(workitem, sort_keys=True)))
        return workitem['hitl_id']

    def get(self, hitl_id: str) -> Optional[dict]:
        row = self.db.execute(
            'SELECT body FROM workitems WHERE hitl_id = ?', (hitl_id,)).fetchone()
        return json.loads(row['body']) if row else None

    def find_by_request_id(self, request_id: str) -> Optional[dict]:
        row = self.db.execute(
            'SELECT body FROM workitems WHERE request_id = ?', (request_id,)).fetchone()
        return json.loads(row['body']) if row else None

    def list_by_status(self, status: str) -> list:
        rows = self.db.execute(
            'SELECT body FROM workitems WHERE status = ? ORDER BY created_at',
            (status,)).fetchall()
        return [json.loads(r['body']) for r in rows]

    def transition(self, hitl_id: str, expected_status: str, new_status: str,
                   decision_record: Optional[dict]):
        """Compare-and-swap transition; raises on concurrent conflict."""
        workitem = self.get(hitl_id)
        if workitem is None:
            raise KeyError(hitl_id)
        workitem['status'] = new_status
        if decision_record is not None:
            workitem['decision'] = decision_record
        cur = self.db.execute(
            'UPDATE workitems SET status = ?, body = ? '
            'WHERE hitl_id = ? AND status = ?',
            (new_status, json.dumps(workitem, sort_keys=True), hitl_id,
             expected_status if expected_status else workitem['status']))
        if cur.rowcount != 1:
            raise RuntimeError(
                f'concurrent transition conflict on {hitl_id} '
                f'(expected {expected_status})')


class SqliteAuditSink:
    """Append-only audit sink with query and retention (public behavior)."""

    def __init__(self, backend: SqliteBackend):
        self.db = backend

    def append(self, event: AuditEvent) -> None:
        self.db.execute(
            'INSERT INTO audit_events (event_id, event_type, occurred_at, '
            'request_id, actor, scope, payload) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (event.event_id, event.event_type, event.occurred_at,
             event.request_id, event.actor, event.scope,
             json.dumps(event.payload, sort_keys=True, default=str)))

    def query(self, request_id: Optional[str] = None,
              event_type: Optional[str] = None,
              since: Optional[str] = None,
              until: Optional[str] = None,
              limit: int = 500) -> list:
        sql = ('SELECT event_id, event_type, occurred_at, request_id, actor, '
               'scope, payload FROM audit_events WHERE 1=1')
        args = []
        if request_id:
            sql += ' AND request_id = ?'
            args.append(request_id)
        if event_type:
            sql += ' AND event_type = ?'
            args.append(event_type)
        if since:
            sql += ' AND occurred_at >= ?'
            args.append(since)
        if until:
            sql += ' AND occurred_at <= ?'
            args.append(until)
        sql += ' ORDER BY seq LIMIT ?'
        args.append(int(limit))
        rows = self.db.execute(sql, tuple(args)).fetchall()
        return [{**{k: r[k] for k in ('event_id', 'event_type', 'occurred_at',
                                      'request_id', 'actor', 'scope')},
                 'payload': json.loads(r['payload'])} for r in rows]

    def purge_older_than(self, cutoff_iso: str) -> int:
        """Retention: the single sanctioned deletion path."""
        cur = self.db.execute(
            'DELETE FROM audit_events WHERE occurred_at < ?', (cutoff_iso,))
        return cur.rowcount


class SqliteDeliveryStore:
    """Persisted callback delivery records — new public contract (OSS-004):
    stable delivery ids, visible failures, idempotent retries."""

    def __init__(self, backend: SqliteBackend):
        self.db = backend

    def create(self, delivery_id: str, request_id: str, hitl_id: str,
               payload: dict, now_iso: str) -> None:
        self.db.execute(
            'INSERT OR IGNORE INTO callback_deliveries (delivery_id, request_id, '
            'hitl_id, status, attempts, created_at, updated_at, payload) '
            "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)",
            (delivery_id, request_id, hitl_id, now_iso, now_iso,
             json.dumps(payload, sort_keys=True, default=str)))

    def get(self, delivery_id: str) -> Optional[dict]:
        row = self.db.execute(
            'SELECT * FROM callback_deliveries WHERE delivery_id = ?',
            (delivery_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        record['payload'] = json.loads(record['payload'])
        return record

    def list_undelivered(self) -> list:
        rows = self.db.execute(
            "SELECT delivery_id FROM callback_deliveries WHERE status != 'delivered' "
            'ORDER BY created_at').fetchall()
        return [r['delivery_id'] for r in rows]

    def mark(self, delivery_id: str, status: str, now_iso: str,
             error: Optional[str] = None) -> None:
        self.db.execute(
            'UPDATE callback_deliveries SET status = ?, attempts = attempts + 1, '
            'last_error = ?, updated_at = ? WHERE delivery_id = ?',
            (status, error, now_iso, delivery_id))
