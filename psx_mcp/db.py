"""Postgres access for our own persisted data (watchlist, alerts, ...).

Separate from client.py, which talks to the PSX portal. Uses psycopg (v3)
async: Neon's pooled connection string runs PgBouncer in transaction mode,
which psycopg3's default (unnamed statement) execution handles correctly,
unlike asyncpg's default prepared-statement behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("psx_mcp.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_loop: asyncio.AbstractEventLoop | None = None
_conn: psycopg.AsyncConnection | None = None
_lock: asyncio.Lock | None = None


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


async def _get_conn() -> psycopg.AsyncConnection:
    """(Re)connect when the running event loop changes, same reasoning as
    PSXClient._bind: a fresh loop means serverless gave us a new request."""
    global _loop, _conn, _lock
    loop = asyncio.get_running_loop()
    if loop is not _loop or _conn is None or _conn.closed:
        if _conn is not None and not _conn.closed:
            try:
                await _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _loop = loop
        _lock = asyncio.Lock()
        _conn = await psycopg.AsyncConnection.connect(_dsn(), row_factory=dict_row, autocommit=True)
    return _conn


async def fetch(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    conn = await _get_conn()
    async with _lock:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def fetchrow(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = await fetch(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: tuple | dict = ()) -> int:
    """Run a statement, return the affected row count."""
    conn = await _get_conn()
    async with _lock:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount


async def close() -> None:
    global _conn
    if _conn is not None and not _conn.closed:
        await _conn.close()
    _conn = None


async def migrate() -> list[str]:
    """Apply any migrations/*.sql not yet recorded, in filename order."""
    conn = await _get_conn()
    await execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {r["version"] for r in await fetch("SELECT version FROM schema_migrations")}
    ran = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        async with _lock:
            async with conn.cursor() as cur:
                await cur.execute(path.read_text())
                await cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        ran.append(path.name)
        log.info("applied migration %s", path.name)
    return ran


def main() -> None:
    logging.basicConfig(level="INFO")
    ran = asyncio.run(migrate())
    print(f"Applied {len(ran)} migration(s): {ran}" if ran else "No pending migrations.")


if __name__ == "__main__":
    main()
