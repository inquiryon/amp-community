"""Local identity/authorization reference implementation (OSS-004).

Authentication: static API tokens mapped to principals (suitable for local
Community deployments; hosts with real identity systems supply their own
IdentityProvider). Authorization semantics:

- role ``reviewer`` may view and decide reviews within its scope
- role ``viewer`` may view only
- service identities (``agent:*``, ``provider:*``) may create but never decide
- a principal never decides its own request (enforced in HitlService too)
- a principal with a scope only sees items in that scope; a principal with
  scope ``None`` is unrestricted
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Principal:
    principal_id: str
    role: str                 # reviewer | viewer | service
    scope: Optional[str] = None


class LocalIdentityProvider:
    def __init__(self, tokens: dict):
        """``tokens`` maps API token -> Principal."""
        self._tokens = dict(tokens)
        self._principals = {p.principal_id: p for p in self._tokens.values()}

    # -- authentication ---------------------------------------------------
    def authenticate(self, token: str) -> Optional[Principal]:
        for known, principal in self._tokens.items():
            if hmac.compare_digest(str(known), str(token or '')):
                return principal
        return None

    # -- authorization ----------------------------------------------------
    def _get(self, principal_id: str) -> Optional[Principal]:
        return self._principals.get(principal_id)

    def _scope_ok(self, principal: Principal, workitem: dict) -> bool:
        return principal.scope is None or principal.scope == workitem.get('scope')

    def can_view(self, principal_id: str, workitem: dict) -> bool:
        principal = self._get(principal_id)
        if principal is None:
            return False
        if principal.role in ('reviewer', 'viewer'):
            return self._scope_ok(principal, workitem)
        # service identities may view items they created
        return workitem.get('requested_by') == principal_id

    def can_decide(self, principal_id: str, workitem: dict) -> bool:
        principal = self._get(principal_id)
        if principal is None or principal.role != 'reviewer':
            return False
        if principal_id == workitem.get('requested_by'):
            return False
        return self._scope_ok(principal, workitem)
