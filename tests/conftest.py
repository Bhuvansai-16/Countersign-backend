import asyncio
import base64
import os
from pathlib import Path

import pytest

from app import db as dbmod
from app.config import load_settings

BASE_ENV = {
    "PAYPAL_CLIENT_ID": "test-client",
    "PAYPAL_CLIENT_SECRET": "test-secret",
    "DATABASE_URL": "postgresql://unused/unused",
    "MERCHANT_SIGNING_KEY": base64.b64encode(bytes(32)).decode(),
    "ADMIN_PASSWORD": "admin-pass",
    "SESSION_SECRET": "s" * 32,
    "MCP_AGENT_KEYS": "agt_test:test-agent-key",
}


@pytest.fixture
def anyio_backend():
    # psycopg's async mode cannot run on Windows' default Proactor loop.
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture
def settings():
    return load_settings(BASE_ENV)


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    try:
        import pgserver
    except ImportError:
        pytest.skip("TEST_DATABASE_URL not set and pgserver not installed")
    # ponytail: embedded Postgres for local runs; CI/hosted can set TEST_DATABASE_URL instead.
    return pgserver.get_server(Path.home() / ".countersign-pg", cleanup_mode=None).get_uri()


@pytest.fixture
async def db(database_url):
    pool = await dbmod.open_pool(database_url)
    await dbmod.apply_schema(pool)
    rows = await dbmod.fetch(pool, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    if rows:
        names = ", ".join(f'"{r["tablename"]}"' for r in rows)
        await dbmod.execute(pool, f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()
