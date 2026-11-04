import base64

import pytest

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
    return "asyncio"


@pytest.fixture
def settings():
    return load_settings(BASE_ENV)
