import json
from contextlib import contextmanager

import httpx
import pytest

from app.main import create_app
from app.mcp_server import current_agent
from tests.conftest import FakePayPal
from tests.test_mandates import _seed_products

pytestmark = pytest.mark.anyio

TOOLS = {"search_products", "create_mandate", "get_mandate_status", "cancel_mandate", "get_receipt",
         "report_issue", "respond_to_proposal", "get_issue_status"}


def _result(res):
    assert not res.is_error, res.content
    return json.loads(res.content[0].text)


@pytest.fixture
def app(db, settings):
    return create_app(settings, pool=db, paypal=FakePayPal().client)


@contextmanager
def as_agent(agent_id="agt_test"):
    token = current_agent.set(agent_id)
    try:
        yield
    finally:
        current_agent.reset(token)


async def test_tools_listed(app):
    assert {t.name for t in await app.state.mcp.list_tools()} == TOOLS


async def test_search_products(app, db):
    await _seed_products(db)
    with as_agent():
        await _test_search_products(app)


async def _test_search_products(app):
    out = _result(await app.state.mcp.call_tool("search_products", {"query": "sage"}))
    assert [p["sku"] for p in out["products"]] == ["THROW-SAGE"]


async def test_create_mandate_tool_returns_confirm_url(app, db):
    await _seed_products(db)
    with as_agent():
        await _test_create_mandate_tool_returns_confirm_url(app)


async def _test_create_mandate_tool_returns_confirm_url(app):
    out = _result(await app.state.mcp.call_tool("create_mandate", {
        "instruction": "Buy one linen throw in sage under $90", "max_amount": "90.00",
        "items": [{"sku": "THROW-SAGE", "qty": 1}], "attributes": {"color": "sage"}}))
    assert out["confirm_url"].endswith("/confirm/" + out["mandate_id"])
    status = _result(await app.state.mcp.call_tool("get_mandate_status", {"mandate_id": out["mandate_id"]}))
    assert status["status"] == "PENDING_CONFIRMATION"
    receipt = _result(await app.state.mcp.call_tool("get_receipt", {"mandate_id": out["mandate_id"]}))
    assert [e["type"] for e in receipt["events"]] == ["MANDATE_CREATED", "CART_SNAPSHOT"]
    assert receipt["events"][0]["payload"]["agent"]["id"] == "agt_test"


async def test_create_mandate_tool_violations_not_raised(app, db):
    await _seed_products(db)
    with as_agent():
        await _test_create_mandate_tool_violations_not_raised(app)


async def _test_create_mandate_tool_violations_not_raised(app):
    out = _result(await app.state.mcp.call_tool("create_mandate", {
        "instruction": "x", "max_amount": "10.00", "items": [{"sku": "THROW-SAGE", "qty": 1}]}))
    assert out["error"] == "VIOLATIONS"
    assert out["violations"][0]["code"] == "OVER_CAP"


async def test_tool_without_agent_identity_fails(app, db):
    with pytest.raises(Exception, match="search_products"):
        await app.state.mcp.call_tool("search_products", {"query": ""})


async def test_mcp_mount_requires_key(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        r = await c.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={})
        assert r.status_code == 401


async def test_other_agent_cannot_read_or_cancel_mandate(app, db):
    await _seed_products(db)
    with as_agent("agt_owner"):
        out = _result(await app.state.mcp.call_tool("create_mandate", {
            "instruction": "x", "max_amount": "90.00", "items": [{"sku": "THROW-SAGE", "qty": 1}]}))
    with as_agent("agt_intruder"):
        for tool in ("get_receipt", "get_mandate_status", "cancel_mandate"):
            with pytest.raises(Exception, match=tool):
                await app.state.mcp.call_tool(tool, {"mandate_id": out["mandate_id"]})
    with as_agent("agt_owner"):
        assert _result(await app.state.mcp.call_tool("get_mandate_status", {"mandate_id": out["mandate_id"]}))["status"] == "PENDING_CONFIRMATION"
