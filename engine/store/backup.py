"""Table export to Parquet for backups (idempotent: same folder per day, files overwritten)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.engine import Engine

from engine.db.models import Base


def export_tables(engine: Engine, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    with engine.connect() as conn:
        for name, table in Base.metadata.tables.items():
            rows = [dict(r._mapping) for r in conn.execute(select(table))]
            cols = {c.name: [_plain(r[c.name]) for r in rows] for c in table.columns}
            path = folder / f"{name}.parquet"
            pq.write_table(pa.table(cols) if rows else pa.table({c.name: pa.array([], pa.string()) for c in table.columns}), path)
            out.append(path)
    return out


def _plain(v):  # type: ignore[no-untyped-def]
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)
