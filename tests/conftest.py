import asyncio
import os
import time
from pathlib import Path

import pytest

from app import db as dbmod
from app.config import load_settings
from app.receipts import new_signing_seed

BASE_ENV = {
    "PAYPAL_CLIENT_ID": "test-client",
    "PAYPAL_CLIENT_SECRET": "test-secret",
    "DATABASE_URL": "postgresql://unused/unused",
    "MERCHANT_SIGNING_KEY": new_signing_seed(),
    "ADMIN_PASSWORD": "admin-pass",
    "SESSION_SECRET": "s" * 32,
    "MCP_AGENT_KEYS": "agt_test:test-agent-key",
}


class FakePayPal:
    """PayPalClient over httpx.MockTransport. routes: {(METHOD, path_prefix): dict | list | callable}."""

    BASE = "https://api-m.sandbox.paypal.com"

    def __init__(self, routes=None):
        import httpx

        from app.paypal import PayPalClient

        self.routes = dict(routes or {})
        self.calls = []
        self.client = PayPalClient(self.BASE, "id", "secret", transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        import httpx

        self.calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 32400})
        matches = [k for k in self.routes if k[0] == request.method and request.url.path.startswith(k[1])]
        if not matches:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "path": request.url.path})
        key = max(matches, key=lambda k: len(k[1]))
        resp = self.routes[key]
        if isinstance(resp, list):
            resp = resp.pop(0) if len(resp) > 1 else resp[0]
        if callable(resp):
            return resp(request)
        if isinstance(resp, httpx.Response):
            return resp
        return httpx.Response(200, json=resp)

    def calls_to(self, method, prefix):
        return [c for c in self.calls if c.method == method and c.url.path.startswith(prefix)]


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
    # After an unclean shutdown, crash recovery can outlast pgserver's 10 s start timeout once: retry.
    for attempt in range(3):
        try:
            return pgserver.get_server(Path.home() / ".countersign-pg", cleanup_mode=None).get_uri()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(10)  # give crash recovery time to finish before the next start attempt


@pytest.fixture
async def db(database_url):
    pool = await dbmod.open_pool(database_url)
    await dbmod.apply_schema(pool)
    rows = await dbmod.fetch(pool, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename NOT LIKE 'checkpoint%%'")
    if rows:
        names = ", ".join(f'"{r["tablename"]}"' for r in rows)
        await dbmod.execute(pool, f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()
