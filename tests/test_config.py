import httpx
import pytest

from app.config import load_settings
from app.main import create_app
from tests.conftest import BASE_ENV


def test_missing_required_vars_named():
    with pytest.raises(RuntimeError) as exc:
        load_settings({})
    assert "PAYPAL_CLIENT_ID" in str(exc.value)
    assert "SESSION_SECRET" in str(exc.value)


def test_agent_keys_parsed():
    s = load_settings({**BASE_ENV, "MCP_AGENT_KEYS": "agt_a:k1,agt_b:k2"})
    assert s.mcp_agent_keys == {"k1": "agt_a", "k2": "agt_b"}


def test_bool_flags():
    assert load_settings({**BASE_ENV, "SIMULATOR_ENABLED": "TRUE"}).simulator_enabled is True
    assert load_settings(BASE_ENV).simulator_enabled is False


@pytest.mark.anyio
async def test_healthz(settings):
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["paypal_token_ttl"] == 0
