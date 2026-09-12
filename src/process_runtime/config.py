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

    create_schema_on_startup: bool = False

    def resolved_iam_public_key(self) -> str:
        if self.iam_public_key:
            return self.iam_public_key
        if self.iam_public_key_file:
            return Path(self.iam_public_key_file).read_text()
        return ""


__all__ = ["Settings"]
