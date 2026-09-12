"""Settings of process-runtime (env prefix ``PR_``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PR_", env_file=None, extra="ignore")

    database_url: str = "postgresql+psycopg://process:process@localhost:5437/process"

    # Trusted IAM context (superproject ADR-0013/0030): the service verifies short-lived
    # audience-bound access tokens issued by iam-service and never issues credentials.
    iam_issuer: str = "http://localhost:8010"
    iam_audience: str = "process-runtime"
    iam_public_key: str = ""
    iam_public_key_file: str = ""
    iam_jwks_url: str = ""

    # BPMN/DMN catalogue: ``definitions.json`` inside ``definitions_dir`` lists the
    # declarations; bpmn/dmn paths are relative to that directory.
    definitions_dir: str = "definitions"
    adapter: str = Field(default="spiff", description="Engine adapter: 'spiff' or 'dummy'.")
    script_task_sandbox: str = Field(
        default="strict", description="Script-task sandbox mode: strict | relaxed | disabled."
    )
    allow_sandbox_disabled: bool = False

    # Timer worker runs inside the API process (asyncio loop); disable for tests.
    timer_worker_enabled: bool = True
    timer_tick_seconds: int = Field(default=15, ge=1, le=300)
    timer_due_batch_size: int = Field(default=100, ge=1)

    # Control Plane bridge (superproject ADR-0023 §3-5, ADR-0032): the service acts in
    # the Control Plane as its own service account (IAM client credentials, audience
    # control-plane). ``auto`` = on when the account and workspace are configured.
    cp_bridge: str = "auto"  # auto | on | off
    cp_base_url: str = "http://localhost:8000"
    iam_base_url: str = "http://localhost:8010"
    iam_client_id: str = ""
    iam_client_secret: str = ""
    cp_workspace_id: str = ""
    cp_task_type_key: str = ""
    # Instances of this IAM tenant are materialised as Control Plane tasks; the bridge
    # acts for one tenant (the one its service account belongs to).
    bridge_tenant_id: str = ""
    bridge_poll_seconds: float = Field(default=3.0, ge=0.2)
    bridge_batch_size: int = Field(default=100, ge=1, le=500)

    create_schema_on_startup: bool = False

    def bridge_enabled(self) -> bool:
        configured = bool(
            self.iam_client_id
            and self.iam_client_secret
            and self.cp_workspace_id
            and self.bridge_tenant_id
        )
        if self.cp_bridge == "off":
            return False
        if self.cp_bridge == "on":
            if not configured:
                raise ValueError(
                    "PR_CP_BRIDGE=on requires PR_IAM_CLIENT_ID, PR_IAM_CLIENT_SECRET, "
                    "PR_CP_WORKSPACE_ID and PR_BRIDGE_TENANT_ID"
                )
            return True
        if self.cp_bridge != "auto":
            raise ValueError(f"unknown PR_CP_BRIDGE: {self.cp_bridge!r}")
        return configured

    def resolved_iam_public_key(self) -> str:
        if self.iam_public_key:
            return self.iam_public_key
        if self.iam_public_key_file:
            return Path(self.iam_public_key_file).read_text()
        return ""


__all__ = ["Settings"]
