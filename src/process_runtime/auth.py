"""Trusted IAM context on the HTTP edge (superproject ADR-0013/0030).

process-runtime is a resource service: it accepts only a short-lived audience-bound access
token issued by iam-service (audience ``process-runtime``) and derives tenant, principal
and scopes from the verified claims. Verification (JWKS, issuer, audience, time claims) is
the shared ``platform-auth-sdk``; here is only the projection into the caller model.

Scopes (declared in the IAM audience registry, see ``deploy/bootstrap.py``):

* ``process:read``  — definitions, instances, tasks, inbox, events;
* ``process:write`` — start instances, complete tasks, signals, cancel;
* ``process:admin`` — forced migration and acting on any human task regardless of
  assignee (the platform operator and, later, the Control Plane reconciliation).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from platform_auth.context import TrustedAuthContext
from platform_auth.errors import InvalidToken, VerificationUnavailable
from platform_auth.jwks import JwksCache, StaticKeySet
from platform_auth.verify import KeySource, TokenVerifier, VerifierConfig

SCOPE_READ = "process:read"
SCOPE_WRITE = "process:write"
SCOPE_ADMIN = "process:admin"


class IamContextError(Exception):
    """Token missing, malformed or not for our audience -> 401."""


class IamVerificationUnavailable(Exception):
    """No key source or JWKS unreachable: the service must deny, never allow -> 503."""


@dataclass(frozen=True)
class Caller:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID | None
    principal_type: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def is_admin(self) -> bool:
        return SCOPE_ADMIN in self.scopes

    @classmethod
    def from_trusted(cls, ctx: TrustedAuthContext) -> Caller:
        return cls(
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            principal_type=ctx.principal_type or "principal",
            scopes=frozenset(ctx.scopes),
        )


class IamContextVerifier:
    """Verify an IAM access token through the SDK; built once per application."""

    def __init__(self, *, issuer: str, audience: str, public_key_pem: str, jwks_url: str) -> None:
        keys: KeySource | None = None
        if public_key_pem:
            keys = StaticKeySet(public_key_pem)
        elif jwks_url:
            keys = JwksCache(jwks_url)
        self._verifier: TokenVerifier | None = None
        if keys is not None and issuer and audience:
            self._verifier = TokenVerifier(keys, VerifierConfig(issuer=issuer, audience=audience))

    async def verify(self, token: str) -> Caller:
        if self._verifier is None:
            raise IamVerificationUnavailable("iam_verifier_not_configured")
        if not token:
            raise IamContextError("missing_token")
        try:
            trusted = await self._verifier.verify(token)
        except InvalidToken as exc:
            raise IamContextError(exc.audit_reason) from exc
        except VerificationUnavailable as exc:
            raise IamVerificationUnavailable(exc.audit_reason) from exc
        return Caller.from_trusted(trusted)


__all__ = [
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "Caller",
    "IamContextError",
    "IamContextVerifier",
    "IamVerificationUnavailable",
]
