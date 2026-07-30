"""Community REST API (OSS-005) — stdlib-only HTTP server.

Design constraints (deliberate):

- Zero dependencies beyond amp-core/amp-community (stdlib ``http.server`` +
  ``sqlite3``): the Community supply chain stays auditable and startup can
  never import Enterprise code or contact proprietary services.
- Bearer-token authentication against the local identity provider;
  authorization decisions (view/decide/create) are delegated to it.
- Every error is the v0 ``ErrorEnvelope``; every route is implementation-
  neutral (no autonomy stages, no provider internals).
- Binds 127.0.0.1 by default; binding elsewhere prints a security warning
  (default configuration is safe for local use).

Routes are documented in ``openapi.json`` (served at /api/v0/openapi.json),
which is the SDK compatibility boundary.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from amp_core import run_eval_policy
from amp_core.schema import validate

from ..hitl import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

MAX_BODY_BYTES = 1_000_000
_STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
_OPENAPI_PATH = os.path.join(os.path.dirname(__file__), 'openapi.json')
_POLICY_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ApiContext:
    """Everything the handler needs; built once at startup."""

    def __init__(self, service, identity, version: str = '0.0.1'):
        self.service = service
        self.identity = identity
        self.version = version


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, indent=1, default=str).encode('utf-8')


class ApiHandler(BaseHTTPRequestHandler):
    server_version = 'amp-community'
    protocol_version = 'HTTP/1.1'

    # quiet default request logging; the audit trail is the record
    def log_message(self, fmt, *args):
        pass

    @property
    def ctx(self) -> ApiContext:
        return self.server.ctx  # type: ignore[attr-defined]

    # -- plumbing ---------------------------------------------------------
    def _send(self, status: int, payload, content_type='application/json'):
        body = _json_bytes(payload) if content_type == 'application/json' else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if content_type.startswith('text/html'):
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str):
        self._send(status, {'error': {'code': code, 'message': message}})

    def _body(self) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, 'payload_too_large', 'request body too large')
        raw = self.rfile.read(length) if length else b''
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, 'invalid_json', f'body is not valid JSON: {exc}')
        if not isinstance(data, dict):
            raise ApiError(400, 'invalid_json', 'body must be a JSON object')
        return data

    def _principal(self):
        auth = str(self.headers.get('Authorization') or '')
        if not auth.startswith('Bearer '):
            raise ApiError(401, 'unauthenticated', 'missing bearer token')
        principal = self.ctx.identity.authenticate(auth[len('Bearer '):].strip())
        if principal is None:
            raise ApiError(401, 'unauthenticated', 'unknown token')
        return principal

    # -- dispatch ---------------------------------------------------------
    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._route(method, path, query)
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except NotFoundError as exc:
            self._error(404, 'not_found', str(exc))
        except AuthorizationError as exc:
            self._error(403, 'forbidden', str(exc))
        except ConflictError as exc:
            self._error(409, 'conflict', str(exc))
        except ValidationError as exc:
            self._error(422, 'validation_failed', str(exc))
        except Exception as exc:  # never leak a traceback to the client
            self._error(500, 'internal_error', f'{type(exc).__name__}')

    def _route(self, method: str, path: str, query: dict):
        ctx = self.ctx

        # ---- unauthenticated ----
        if method == 'GET' and path == '/health':
            return self._send(200, {'status': 'ok'})
        if method == 'GET' and path == '/version':
            return self._send(200, {'name': 'amp-community', 'version': ctx.version,
                                    'contracts': 'v0'})
        if method == 'GET' and path == '/api/v0/openapi.json':
            with open(_OPENAPI_PATH, encoding='utf-8') as f:
                return self._send(200, json.load(f))
        if method == 'GET' and path in ('/', '/ui', '/ui/'):
            return self._static('index.html')
        if method == 'GET' and path.startswith('/ui/'):
            return self._static(path[len('/ui/'):])

        # ---- authenticated ----
        principal = self._principal()
        pid = principal.principal_id
        service = ctx.service

        if method == 'GET' and path == '/api/v0/me':
            return self._send(200, {'principal_id': pid, 'role': principal.role,
                                    'scope': principal.scope})

        # policies
        if path == '/api/v0/policies' and method == 'GET':
            return self._send(200, {'items': service.policies.list_policies()})
        m = re.match(r'^/api/v0/policies/([^/]+)$', path)
        if m:
            policy_id = self._policy_id(m.group(1))
            if method == 'GET':
                policy = service.policies.get_policy(policy_id)
                if policy is None:
                    raise ApiError(404, 'not_found', f'no policy {policy_id}')
                return self._send(200, policy)
            if method == 'PUT':
                if principal.role != 'reviewer':
                    raise ApiError(403, 'forbidden', 'only reviewers manage policies')
                policy = self._body()
                errors = validate(policy, 'Policy')
                if errors:
                    return self._send(422, {'error': {
                        'code': 'invalid_policy', 'message': 'schema validation failed',
                        'details': {'errors': errors}}})
                service.policies.put_policy(policy_id, policy)
                return self._send(200, {'policy_id': policy_id, 'stored': True})
        m = re.match(r'^/api/v0/policies/([^/]+)/validate$', path)
        if m and method == 'POST':
            errors = validate(self._body(), 'Policy')
            return self._send(200, {'valid': not errors, 'errors': errors})
        m = re.match(r'^/api/v0/policies/([^/]+)/evaluate$', path)
        if m and method == 'POST':
            # Dry-run tester: evaluates without creating a request or review.
            policy_id = self._policy_id(m.group(1))
            policy = service.policies.get_policy(policy_id)
            if policy is None:
                raise ApiError(404, 'not_found', f'no policy {policy_id}')
            body = self._body()
            result = run_eval_policy(
                policy, str(body.get('tool') or '*'), str(body.get('action') or '*'),
                body.get('params') or {},
                criteria_inputs=body.get('criteria_inputs'))
            return self._send(200, {
                'decision': result['decision'], 'reason': result['reason'],
                'triggered_criteria': [c.get('criterion_id')
                                       for c in result.get('triggered_criteria', [])],
                'soft_triggered': [c.get('criterion_id')
                                   for c in result.get('soft_triggered', [])],
                'criteria_results': [
                    {k: r.get(k) for k in ('criterion_id', 'hardness',
                                           'evaluator_type', 'status',
                                           'condition_semantics', 'condition_met',
                                           'triggered', 'trigger_reason', 'reason')}
                    for r in result.get('criteria_results', [])],
            })

        # governed requests
        if path == '/api/v0/requests' and method == 'POST':
            body = self._body()
            for field in ('policy_id', 'tool', 'action'):
                if not body.get(field):
                    raise ApiError(400, 'missing_field', f'{field} is required')
            out = service.submit_request(
                str(body['policy_id']), str(body['tool']), str(body['action']),
                body.get('params') or {}, requested_by=pid,
                request_id=body.get('request_id'),
                criteria_inputs=body.get('criteria_inputs'),
                scope=principal.scope)
            return self._send(200, out)
        m = re.match(r'^/api/v0/requests/([^/]+)$', path)
        if m and method == 'GET':
            return self._send(200, self._request_status(pid, m.group(1)))

        # HITL
        if path == '/api/v0/hitl' and method == 'GET':
            items = service.hitl.list_pending(pid)
            return self._send(200, {'items': items})
        m = re.match(r'^/api/v0/hitl/([^/]+)$', path)
        if m and method == 'GET':
            return self._send(200, service.hitl.get(pid, m.group(1)))
        m = re.match(r'^/api/v0/hitl/([^/]+)/resolve$', path)
        if m and method == 'POST':
            body = self._body()
            decision = str(body.get('decision') or '')
            if decision not in ('approve', 'reject', 'modify'):
                raise ApiError(400, 'invalid_decision',
                               "decision must be approve, reject, or modify")
            out = service.resolve(pid, m.group(1), decision,
                                  modified_params=body.get('modified_params'),
                                  note=str(body.get('note') or ''))
            return self._send(200, out)

        # callbacks + audit
        if path == '/api/v0/callbacks/retry' and method == 'POST':
            if principal.role != 'reviewer':
                raise ApiError(403, 'forbidden', 'only reviewers retry callbacks')
            return self._send(200, {'results': service.retry_callbacks()})
        if path == '/api/v0/audit' and method == 'GET':
            events = service.audit.query(
                request_id=(query.get('request_id') or [None])[0],
                event_type=(query.get('event_type') or [None])[0],
                since=(query.get('since') or [None])[0],
                until=(query.get('until') or [None])[0],
                limit=int((query.get('limit') or ['200'])[0]))
            return self._send(200, {'items': events})

        raise ApiError(404, 'not_found', f'no route {method} {path}')

    # -- helpers ----------------------------------------------------------
    def _policy_id(self, raw: str) -> str:
        policy_id = urllib.parse.unquote(raw)
        if not _POLICY_ID_RE.match(policy_id):
            raise ApiError(400, 'invalid_policy_id', 'policy id has invalid characters')
        return policy_id

    def _request_status(self, principal_id: str, request_id: str) -> dict:
        """Expose the decision to the calling agent (poll model)."""
        workitem = self.ctx.service.hitl.store.find_by_request_id(request_id)
        if workitem is None:
            raise ApiError(404, 'not_found', f'no reviewed request {request_id} '
                           '(allowed/denied requests complete synchronously)')
        if not self.ctx.identity.can_view(principal_id, workitem):
            raise ApiError(403, 'forbidden', 'not visible to this principal')
        decision = workitem.get('decision') or {}
        return {
            'request_id': request_id,
            'hitl_id': workitem['hitl_id'],
            'status': workitem['status'],
            'decision': decision.get('decision'),
            'executed_params': decision.get('executed_params',
                                            workitem['params']
                                            if workitem.get('status') in
                                            ('approved', 'resumed') else None),
            'follow_up_hitl_id': decision.get('follow_up_hitl_id'),
        }

    def _static(self, name: str):
        safe = os.path.normpath(name).lstrip('/')
        if safe.startswith('..') or safe == '.':
            raise ApiError(404, 'not_found', 'no such asset')
        path = os.path.join(_STATIC_DIR, safe)
        if not os.path.isfile(path):
            raise ApiError(404, 'not_found', 'no such asset')
        content_type = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
        }.get(os.path.splitext(path)[1], 'application/octet-stream')
        with open(path, 'rb') as f:
            self._send(200, f.read(), content_type)


def make_server(ctx: ApiContext, host: str = '127.0.0.1', port: int = 8080):
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.ctx = ctx  # type: ignore[attr-defined]
    return server


def serve_forever(ctx: ApiContext, host: str = '127.0.0.1', port: int = 8080):
    server = make_server(ctx, host, port)
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print(f'[amp-community] WARNING: binding {host} exposes the API beyond '
              'this machine. Use a TLS-terminating reverse proxy and strong '
              'tokens before external exposure.')
    print(f'[amp-community] listening on http://{host}:{server.server_address[1]} '
          f'(UI at /, OpenAPI at /api/v0/openapi.json)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return server


def start_background(ctx: ApiContext, host: str = '127.0.0.1', port: int = 0):
    """Start on a background thread (tests); returns (server, port)."""
    server = make_server(ctx, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]
