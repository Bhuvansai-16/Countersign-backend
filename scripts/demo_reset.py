"""Clear dispute-handling state before recording a demo. Keeps products, receipts, orders and disputes.

Usage (repo root): uv run --env-file .env python scripts/demo_reset.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402


async def main():
    pool = await db.open_pool(os.environ["DATABASE_URL"])
    await db.execute(pool, "DELETE FROM approvals WHERE dispute_id IS NOT NULL")
    await db.execute(pool, "DELETE FROM cases")
    await db.execute(pool, "DELETE FROM webhook_events")
    tables = [r["tablename"] for r in await db.fetch(pool, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                                           "AND tablename IN ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes')")]
    if tables:
        await db.execute(pool, "TRUNCATE " + ", ".join(tables))
    print("reset: cases, dispute approvals, webhook events and agent checkpoints cleared")
    await pool.close()


asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
