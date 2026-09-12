"""Alembic chain: upgrade → downgrade → upgrade on a real PostgreSQL."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from tests.conftest import ROOT

pytestmark = [pytest.mark.db]

EXPECTED = {
    "workflow_instances",
    "workflow_transition_log",
    "workflow_timers",
    "workflow_tasks",
    "process_events",
}


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_downgrade_roundtrip(migrated_database: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", migrated_database)
    assert _tables(migrated_database) >= EXPECTED
    command.downgrade(config, "base")
    assert not EXPECTED & _tables(migrated_database)
    command.upgrade(config, "head")
    assert _tables(migrated_database) >= EXPECTED
