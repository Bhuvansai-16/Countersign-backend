from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")


async def open_pool(url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, kwargs={"row_factory": dict_row, "autocommit": True}, open=False)
    await pool.open()
    return pool


async def apply_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))


async def fetch(pool, sql: str, *args) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args or None)
        return await cur.fetchall()


async def fetchrow(pool, sql: str, *args) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args or None)
        return await cur.fetchone()


async def execute(pool, sql: str, *args) -> None:
    async with pool.connection() as conn:
        await conn.execute(sql, args or None)
