"""Community API composition (OSS-005).

``build_api_context`` is the Community composition root for the HTTP
deployment: it registers public identity, storage, evaluation, decision,
callback, and audit providers and never discovers, imports, or
license-checks Enterprise implementations.
"""

from __future__ import annotations

import json
import os

from inquiryon_amp_core import FilePolicyStore

from ..identity import Principal
from ..service import build_local_service
from .server import ApiContext, make_server, serve_forever, start_background

__all__ = ['ApiContext', 'build_api_context', 'load_config', 'make_server',
           'serve_forever', 'start_background']


class PollCallback:
    """Default delivery mode: no outbound call; agents poll
    GET /api/v0/requests/{request_id}. Deliveries are recorded as delivered
    immediately (the persisted decision is the exposed result)."""

    def deliver(self, delivery_id, request_id, result):
        return None


def load_config(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError('config must be a JSON object')
    if not config.get('tokens'):
        raise ValueError(
            'config.tokens is required: [{"token", "principal", "role", "scope"?}]. '
            'Generate strong random tokens; see examples/config.example.json')
    return config


def build_api_context(config: dict) -> ApiContext:
    tokens = {}
    for entry in config['tokens']:
        role = entry.get('role', 'viewer')
        if role not in ('reviewer', 'viewer', 'service'):
            raise ValueError(f'unknown role {role!r} for {entry.get("principal")}')
        tokens[entry['token']] = Principal(
            str(entry['principal']), role, entry.get('scope'))

    callback_config = config.get('callback') or {'mode': 'poll'}
    if callback_config.get('mode') == 'http':
        from ..callbacks import HttpAgentCallback
        callback = HttpAgentCallback(callback_config['url'])
    else:
        callback = PollCallback()

    policy_store = FilePolicyStore(config.get('policy_dir', './policies'))
    service = build_local_service(
        policy_store, tokens, callback,
        db_path=config.get('db_path', './amp-community.db'))
    identity = service.hitl.identity
    from .. import __version__
    return ApiContext(service, identity, version=__version__)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog='python -m inquiryon_amp_community.api',
        description='AMP Community Edition API + UI (experimental v0)')
    parser.add_argument('--config', default=os.environ.get(
        'AMP_COMMUNITY_CONFIG', 'config.json'))
    parser.add_argument('--host', default=None, help='override config bind host')
    parser.add_argument('--port', type=int, default=None, help='override config port')
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        ctx = build_api_context(config)
    except (OSError, ValueError, KeyError) as exc:
        print(f'[amp-community] startup failed: {exc}')
        return 1
    serve_forever(ctx,
                  host=args.host or config.get('bind', '127.0.0.1'),
                  port=args.port if args.port is not None else int(config.get('port', 8080)))
    return 0
