import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app import db as dbmod, receipts, shopping
from app import auth
from app.main import create_app
from tests.conftest import FakePayPal

pytestmark = pytest.mark.anyio

PRODUCT = {"id": "p1", "title": "Sage linen throw", "description": "Linen throw",
           "structured_attributes": {"color": "sage", "material": "linen"},
           "offers": [{"url": "https://shop.example.com/throw", "domain": "shop.example.com",
                       "price": {"price": 78, "currency": "USD"}, "availability": "InStock"}]}


def providers(settings, handler):
    settings = replace(settings, demo_enabled=True, channel3_api_key="channel-secret", kernel_api_key="kernel-secret", llm_model="test-model")
    return settings, shopping.Providers(settings, transport=httpx.MockTransport(handler))


async def test_search_uses_real_contract_and_stores_snapshot(db, settings):
    def handler(req):
        assert str(req.url) == "https://api.trychannel3.com/v1/search"
        assert req.headers["x-api-key"] == "channel-secret"
        assert json.loads(req.content)["query"] == "sage linen throw"
        return httpx.Response(200, json={"products": [PRODUCT]})
    settings, client = providers(settings, handler)
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    result = await shopping.search(s, "agent-a", "sage linen throw", "90.00")
    candidate = result["products"][0]
    assert candidate["price"] == "78.00"
    assert candidate["attributes"]["color"] == "sage"
    stored = await shopping.candidate(s, "agent-a", candidate["candidate_id"])
    assert stored["snapshot"]["source_product_id"] == "p1"
    with pytest.raises(HTTPException) as exc:
        await shopping.candidate(s, "agent-b", candidate["candidate_id"])
    assert exc.value.status_code == 404


async def test_unusable_offers_are_not_purchasable():
    for patch in ({"price": {"price": "NaN", "currency": "USD"}},
                  {"url": "http://127.0.0.1/admin"}, {"availability": "OutOfStock"},
                  {"price": {"price": 78, "currency": "EUR"}}):
        product = {**PRODUCT, "offers": [{**PRODUCT["offers"][0], **patch}]}
        assert shopping.normalize(product) is None
    missing = shopping.normalize({**PRODUCT, "structured_attributes": {}})
    assert missing["attributes"] == {}


async def test_provider_errors_do_not_leak_secrets(settings):
    settings, client = providers(settings, lambda _: httpx.Response(401, text="channel-secret"))
    with pytest.raises(HTTPException) as exc:
        await client.request("channel3", "POST", "/v1/search", {})
    assert exc.value.status_code == 502
    assert "channel-secret" not in str(exc.value.detail)


async def test_sandbox_mandate_enforces_constraints_and_records_provenance(db, settings):
    settings, client = providers(settings, lambda _: httpx.Response(200, json={"products": [PRODUCT]}))
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    cid = (await shopping.search(s, "agent-a", "throw", "90"))["products"][0]["candidate_id"]
    for cap, attrs, code in [("80", {}, "OVER_CAP"), ("90", {"color": "blue"}, "ATTRIBUTE_MISMATCH")]:
        with pytest.raises(shopping.m.MandateError) as exc:
            await shopping.prepare(s, "agent-a", cid, "Buy a throw", cap, attrs)
        assert exc.value.violations[0]["code"] == code
    out = await shopping.prepare(s, "agent-a", cid, "Buy a sage throw under $90", "90", {"color": "sage"})
    assert out["quote"]["total"] == "84.00"
    rec = await receipts.load_receipt(db, out["mandate_id"])
    assert rec["status"] == "PENDING_CONFIRMATION"
    assert rec["cart"]["items"][0]["name"].startswith("[SANDBOX COPY]")
    events = await receipts.load_events(db, out["mandate_id"])
    event = next(e for e in events if e["type"] == "PRODUCT_DISCOVERY")
    assert event["payload"]["sandbox_copy"] is True
    assert event["payload"]["source"]["source_product_id"] == "p1"
    assert (await dbmod.fetchrow(db, "SELECT count(*) AS n FROM orders"))["n"] == 0


async def test_kernel_navigation_is_fixed_read_only_and_session_can_be_stopped(db, settings):
    calls = []
    def handler(req):
        calls.append(req)
        if req.url.host == "api.trychannel3.com":
            return httpx.Response(200, json={"products": [PRODUCT]})
        assert req.headers["authorization"] == "Bearer kernel-secret"
        if req.url.path == "/browsers":
            assert json.loads(req.content)["timeout_seconds"] == 300
            return httpx.Response(201, json={"session_id": "session1", "browser_live_view_url": "https://prod-iad-hypeman-29.kernel.sh:8443/browser/live/token"})
        if req.method == "DELETE":
            return httpx.Response(204)
        code = json.loads(req.content)["code"]
        assert "https://shop.example.com/throw" in code
        assert ".click(" not in code and ".fill(" not in code
        return httpx.Response(200, json={"success": True, "result": {"title": "Sage throw", "url": "https://shop.example.com/throw", "text": "Linen"}})
    settings, client = providers(settings, handler)
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    cid = (await shopping.search(s, "agent-a", "throw", "90"))["products"][0]["candidate_id"]
    browser = await shopping.start_browser(s, "agent-a", cid)
    assert "readOnly=true" in browser["live_view_url"]
    assert await shopping.start_browser(s, "agent-a", cid) == browser
    assert len([r for r in calls if r.url.path == "/browsers"]) == 1
    assert (await shopping.inspect(s, "agent-a", cid))["title"] == "Sage throw"
    await shopping.stop_browser(s, "agent-a", cid)
    assert (await shopping.candidate(s, "agent-a", cid))["browser"] is None
    assert calls[-1].method == "DELETE"


async def test_kernel_execution_failure_is_not_reported_as_success(db, settings):
    def handler(req):
        if req.url.host == "api.trychannel3.com":
            return httpx.Response(200, json={"products": [PRODUCT]})
        if req.url.path == "/browsers":
            return httpx.Response(201, json={"session_id": "s1", "browser_live_view_url": "https://api.onkernel.com/browser/live/t"})
        return httpx.Response(200, json={"success": False, "error": "page timed out"})
    settings, client = providers(settings, handler)
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    cid = (await shopping.search(s, "a", "throw", "90"))["products"][0]["candidate_id"]
    await shopping.start_browser(s, "a", cid)
    with pytest.raises(HTTPException) as exc:
        await shopping.inspect(s, "a", cid)
    assert exc.value.status_code == 502


async def test_shopping_needs_no_login_but_still_requires_demo_and_sandbox(db, settings):
    for enabled, base, expected in [(False, settings.paypal_base_url, 404), (True, "https://api-m.paypal.com", 404),
                                     (True, settings.paypal_base_url, 200)]:
        app = create_app(replace(settings, demo_enabled=enabled, paypal_base_url=base), pool=db, paypal=FakePayPal().client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            result = await c.get("/api/demo/shopping/status")
            assert result.status_code == expected
            if expected == 200:
                assert result.json() == {"channel3": False, "kernel": False, "sandbox": True}
                assert "httponly" in result.headers["set-cookie"].lower()


async def test_visitors_have_isolated_products_and_no_merchant_access(db, settings):
    settings, provider = providers(settings, lambda _: httpx.Response(200, json={"products": [PRODUCT]}))
    app = create_app(settings, pool=db, paypal=FakePayPal().client)
    app.state.cs.providers = provider
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as a, \
            httpx.AsyncClient(transport=transport, base_url="http://t") as b:
        assert (await a.get("/api/demo/shopping/status")).status_code == 200
        assert (await b.get("/api/demo/shopping/status")).status_code == 200
        assert a.cookies["cs_shopping"] != b.cookies["cs_shopping"]
        found = await a.post("/api/demo/shopping/search", json={"query": "throw", "max_amount": "90"})
        cid = found.json()["products"][0]["candidate_id"]
        payload = {"instruction": "Buy a throw", "max_amount": "90", "attributes": {}}
        assert (await b.post(f"/api/demo/shopping/{cid}/mandate", json=payload)).status_code == 404
        assert (await a.post(f"/api/demo/shopping/{cid}/mandate", json=payload)).status_code == 200
        assert (await a.get("/api/orders")).status_code == 401
        assert (await a.get("/api/orders", headers={"Authorization": "Bearer " + a.cookies["cs_shopping"]})).status_code == 401
        assert (await a.post("/api/demo/shopping/search", json={"query": "throw", "max_amount": "90"},
                             headers={"Origin": "https://unrelated.example"})).status_code == 403


async def test_shopping_cookie_is_signed_expiring_and_not_a_seller_token(settings):
    token = auth.make_shopping_token(settings.session_secret)
    assert auth.shopping_identity(settings.session_secret, token).startswith("visitor_")
    assert auth.shopping_identity(settings.session_secret, token + "changed") is None
    assert auth.shopping_identity(settings.session_secret, auth.make_shopping_token(settings.session_secret, ttl_s=-1)) is None
    assert auth.check_token(settings.session_secret, token) is False


async def test_chat_uses_tools_and_keeps_budget_out_of_model_control(db, settings, monkeypatch):
    from langchain_core.messages import AIMessage
    settings, client = providers(settings, lambda _: httpx.Response(200, json={"products": [PRODUCT]}))
    class Model:
        step = 0
        def bind_tools(self, specs):
            prepare_spec = next(t for t in specs if t["function"]["name"] == "prepare_purchase")
            assert "max_amount" not in prepare_spec["function"]["parameters"]["properties"]
            return self
        async def ainvoke(self, messages):
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[{"name": "find_products", "args": {"query": "sage throw"}, "id": "t1"}])
            found = json.loads(messages[-1].content)["products"][0]
            if self.step == 2:
                return AIMessage(content="I found a match.", tool_calls=[])
            return AIMessage(content="")
    monkeypatch.setattr(shopping, "chat_model", lambda _: Model())
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    out = await shopping.chat(s, "a", [{"role": "user", "content": "Find a sage throw"}], "90", {"color": "sage"})
    assert out["reply"] == "I found a match."
    assert out["products"][0]["title"] == "Sage linen throw"
    assert out["steps"][0]["tool"] == "find_products"
    assert "kernel-secret" not in json.dumps(out)


async def test_chat_cannot_raise_budget_using_extra_tool_arguments(db, settings, monkeypatch):
    from langchain_core.messages import AIMessage
    settings, client = providers(settings, lambda _: httpx.Response(200, json={"products": [PRODUCT]}))
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    cid = (await shopping.search(s, "a", "throw", "90"))["products"][0]["candidate_id"]
    class Model:
        step = 0
        def bind_tools(self, _):
            return self
        async def ainvoke(self, messages):
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[{"name": "prepare_purchase", "args": {"candidate_id": cid, "max_amount": "900"}, "id": "t"}])
            assert json.loads(messages[-1].content)["violations"][0]["code"] == "OVER_CAP"
            return AIMessage(content="The cart exceeds your budget.")
    monkeypatch.setattr(shopping, "chat_model", lambda _: Model())
    result = await shopping.chat(s, "a", [{"role": "user", "content": "Buy a throw"}], "80", {})
    assert result["mandate"] is None
    assert result["steps"] == [{"tool": "prepare_purchase", "ok": False}]


async def test_tiny_amount_and_invalid_public_urls_rejected():
    with pytest.raises(HTTPException):
        shopping.money("0.001")
    for url in ("https://localhost/a", "https://127.0.0.1/a", "https://10.0.0.1/a", "https://host.internal/a",
                "https://user:pass@shop.example.com/a", "javascript:alert(1)"):
        assert shopping.public_url(url) is None


async def test_stop_expired_browser_is_idempotent(db, settings):
    def handler(req):
        if req.url.host == "api.trychannel3.com":
            return httpx.Response(200, json={"products": [PRODUCT]})
        if req.method == "DELETE":
            return httpx.Response(404)
        return httpx.Response(201, json={"session_id": "s1", "browser_live_view_url": "https://api.onkernel.com/browser/live/t"})
    settings, provider = providers(settings, handler)
    s = SimpleNamespace(settings=settings, pool=db, providers=provider)
    cid = (await shopping.search(s, "a", "throw", "90"))["products"][0]["candidate_id"]
    await shopping.start_browser(s, "a", cid)
    assert await shopping.stop_browser(s, "a", cid) == {"stopped": True}


async def test_external_mcp_tools_are_gated_and_owner_scoped(db, settings):
    from tests.test_mcp import as_agent, _result
    settings, provider = providers(settings, lambda _: httpx.Response(200, json={"products": [PRODUCT]}))
    app = create_app(settings, pool=db, paypal=FakePayPal().client)
    app.state.cs.providers = provider
    tools = {t.name for t in await app.state.mcp.list_tools()}
    assert {"search_external_products", "inspect_external_product", "create_external_mandate", "stop_external_browser"} <= tools
    with as_agent("owner"):
        found = _result(await app.state.mcp.call_tool("search_external_products", {"query": "throw", "max_amount": "90"}))
    cid = found["products"][0]["candidate_id"]
    with as_agent("intruder"):
        out = _result(await app.state.mcp.call_tool("create_external_mandate", {
            "candidate_id": cid, "instruction": "Buy throw", "max_amount": "90", "attributes": {}}))
        assert out["error"] == "product discovery not found"


async def test_chat_followup_has_owner_checked_numbered_candidates(db, settings, monkeypatch):
    from langchain_core.messages import AIMessage
    settings, client = providers(settings, lambda _: httpx.Response(200, json={"products": [PRODUCT]}))
    s = SimpleNamespace(settings=settings, pool=db, providers=client)
    cid = (await shopping.search(s, "a", "throw", "90"))["products"][0]["candidate_id"]
    class Model:
        def bind_tools(self, _):
            return self
        async def ainvoke(self, messages):
            assert cid in messages[0].content
            assert '"card_number": 1' in messages[0].content
            assert '"requirements_met": false' in messages[0].content
            return AIMessage(content="Product 1 has sage, which does not meet your required blue.")
    monkeypatch.setattr(shopping, "chat_model", lambda _: Model())
    history = [{"role": "user", "content": "Tell me about product 1"}]
    result = await shopping.chat(s, "a", history, "90", {"color": "blue"}, candidate_ids=[cid])
    assert "Product 1" in result["reply"]
    with pytest.raises(HTTPException) as exc:
        await shopping.chat(s, "other-agent", history, "90", {}, candidate_ids=[cid])
    assert exc.value.status_code == 404


async def test_kernel_security_check_is_not_a_successful_listing_inspection(db, settings):
    def handler(req):
        if req.url.host == "api.trychannel3.com":
            return httpx.Response(200, json={"products": [PRODUCT]})
        if req.url.path == "/browsers":
            return httpx.Response(201, json={"session_id": "s1", "browser_live_view_url": "https://api.onkernel.com/browser/live/t"})
        return httpx.Response(200, json={"success": True, "result": {
            "title": "Retailer product", "url": "https://shop.example.com/throw", "text": "Quick security check. Verify you are human."}})
    settings, provider = providers(settings, handler)
    s = SimpleNamespace(settings=settings, pool=db, providers=provider)
    cid = (await shopping.search(s, "a", "throw", "90"))["products"][0]["candidate_id"]
    await shopping.start_browser(s, "a", cid)
    with pytest.raises(HTTPException, match="security check"):
        await shopping.inspect(s, "a", cid)
