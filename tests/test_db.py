import psycopg
import pytest

from app import db as dbmod

pytestmark = pytest.mark.anyio


async def _seed_event(pool):
    await dbmod.execute(
        pool,
        "INSERT INTO receipts (id, status, instruction, constraints, cart, expires_at) "
        "VALUES ('rcpt_1', 'PENDING_CONFIRMATION', 'buy', '{}', '{}', now())",
    )
    await dbmod.execute(
        pool,
        "INSERT INTO receipt_events (receipt_id, seq, type, payload, at, prev_hash, hash) "
        "VALUES ('rcpt_1', 0, 'MANDATE_CREATED', '{}', '2026-11-04T00:00:00Z', %s, %s)",
        "0" * 64,
        "a" * 64,
    )


async def test_healthz_reports_db(db, settings):
    import httpx

    from app.main import create_app

    app = create_app(settings, pool=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/healthz")).json()
    assert body["db"] is True


async def test_schema_idempotent(db):
    await dbmod.apply_schema(db)
    await dbmod.apply_schema(db)


async def test_receipt_events_append_only(db):
    await _seed_event(db)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await dbmod.execute(db, "UPDATE receipt_events SET type = 'X'")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await dbmod.execute(db, "DELETE FROM receipt_events")
    rows = await dbmod.fetch(db, "SELECT type FROM receipt_events")
    assert rows == [{"type": "MANDATE_CREATED"}]


async def test_pool_survives_server_closing_idle_connections(db):
    """Neon suspends idle databases and closes pooled connections; the next query must still work."""
    await dbmod.fetchrow(db, "SELECT 1 AS ok")
    await dbmod.execute(db, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND pid <> pg_backend_pid()")
    async with db.connection() as conn:  # the connection that ran the terminate is now fine; kill it too
        await conn.execute("SELECT 1")
    await dbmod.execute(db, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND pid <> pg_backend_pid()")
    assert (await dbmod.fetchrow(db, "SELECT 1 AS ok"))["ok"] == 1
