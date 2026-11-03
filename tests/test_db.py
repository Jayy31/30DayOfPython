"""Migration round-trip against a real Postgres. Skipped if DATABASE_URL is unreachable."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from engine.config.settings import Settings
from engine.db.models import Base
from engine.db.session import make_engine

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine():
    eng = make_engine(Settings().database_url)
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    return eng


def test_migrations_roundtrip(engine):
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
