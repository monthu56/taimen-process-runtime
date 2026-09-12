"""The bridge's identity in the Control Plane: the service account of process-runtime.

IAM client credentials are exchanged for a short-lived access token of audience
``control-plane`` by the shared ``ServiceTokenProvider`` (platform-auth-sdk); this
adapter presents it through the ``CredentialProvider`` contract of control-plane-client
so ``ControlPlaneClient`` refreshes on 401 like any other IAM caller.
"""

from __future__ import annotations

from platform_auth import ServiceCredentials, ServiceTokenProvider

CONTROL_PLANE_AUDIENCE = "control-plane"
CONTROL_PLANE_SCOPES = ("control-plane:read", "control-plane:write")


class ServiceAccountCredential:
    """``CredentialProvider`` over ``ServiceTokenProvider``."""

    def __init__(
        self,
        *,
        iam_base_url: str,
        client_id: str,
        client_secret: str,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self._provider = ServiceTokenProvider(
            iam_base_url,
            ServiceCredentials(
                client_id=client_id,
                client_secret=client_secret,
                audience=CONTROL_PLANE_AUDIENCE,
                scopes=CONTROL_PLANE_SCOPES,
            ),
            request_timeout_seconds=request_timeout_seconds,
        )

    @property
    def refreshable(self) -> bool:
        return True

    async def token(self) -> str:
        return await self._provider()

    async def refresh(self) -> str:
        self._provider.forget()
        return await self._provider.refresh()

    async def aclose(self) -> None:
        await self._provider.aclose()


__all__ = ["CONTROL_PLANE_AUDIENCE", "CONTROL_PLANE_SCOPES", "ServiceAccountCredential"]
