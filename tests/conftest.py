"""Shared fixtures.

Two tiers: pure tests (domain, sandbox, adapters, catalogue) run anywhere; tests marked
``db`` need a real PostgreSQL (``PR_TEST_DATABASE_URL``, ``compose.test.yml``) — the store
relies on advisory locks and JSONB operators, so there is no SQLite double. Without the
variable the ``db`` tier is skipped, never faked.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from process_runtime.app import create_app
from process_runtime.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS_DIR = str(ROOT / "definitions")
TEST_DATABASE_URL = os.environ.get("PR_TEST_DATABASE_URL", "")
IAM_ISSUER = "https://iam.example/iam"
IAM_AUDIENCE = "process-runtime"

_TRUNCATE = text(
    "TRUNCATE bridge_cursors, process_task_bindings, process_events, workflow_tasks, "
    "workflow_timers, workflow_transition_log, workflow_instances CASCADE"
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="PR_TEST_DATABASE_URL is not set (see compose.test.yml)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ------------------------------------------------------------------ IAM material
def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


class IamIssuer:
    def __init__(self) -> None:
        self.private_pem, self.public_pem = _keypair()

    def token(
        self,
        tenant_id: uuid.UUID,
        *,
        principal_id: uuid.UUID | None = None,
        scopes: list[str] | None = None,
        principal_type: str = "human",
        audience: str = IAM_AUDIENCE,
        issuer: str = IAM_ISSUER,
        expires_in: int = 300,
    ) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "iss": issuer,
                "sub": str(principal_id or uuid.uuid4()),
                "tenant_id": str(tenant_id),
                "aud": audience,
                "scope": scopes or ["process:read", "process:write"],
                "principal_type": principal_type,
                "credential_id": str(uuid.uuid4()),
                "iat": now,
                "nbf": now,
                "exp": now + timedelta(seconds=expires_in),
                "jti": str(uuid.uuid4()),
            },
            self.private_pem,
            algorithm="RS256",
        )

    def auth(self, tenant_id: uuid.UUID, **kwargs: object) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(tenant_id, **kwargs)}"}  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def iam() -> IamIssuer:
    return IamIssuer()


# ------------------------------------------------------------------- database
@pytest.fixture(scope="session")
def migrated_database() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("PR_TEST_DATABASE_URL is not set")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.exit(
            f"Test PostgreSQL is not reachable at {TEST_DATABASE_URL}: {exc}\n"
            "Start it with: docker compose -f compose.test.yml up -d --wait db-test",
            returncode=3,
        )
    finally:
        engine.dispose()
    os.environ["PR_DATABASE_URL"] = TEST_DATABASE_URL
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    alembic_command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
def clean_database(migrated_database: str) -> Iterator[str]:
    engine = create_engine(migrated_database)
    with engine.begin() as connection:
        connection.execute(_TRUNCATE)
    engine.dispose()
    yield migrated_database


@pytest.fixture
async def session_factory(clean_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(clean_database, poolclass=NullPool)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def seeded_user() -> uuid.UUID:
    return uuid.uuid4()


# ------------------------------------------------------------------- HTTP app
@pytest.fixture
def app_settings(clean_database: str, iam: IamIssuer) -> Settings:
    return Settings(
        database_url=clean_database,
        iam_issuer=IAM_ISSUER,
        iam_audience=IAM_AUDIENCE,
        iam_public_key=iam.public_pem,
        definitions_dir=DEFINITIONS_DIR,
        timer_worker_enabled=False,
    )


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings)) as test_client:
        yield test_client
