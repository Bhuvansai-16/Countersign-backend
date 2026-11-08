import json

import httpx
import pytest

from app import db as dbmod
from app import receipts as r
from app.main import create_app
from tests.conftest import FakePayPal
from tests.test_mandates import CAPTURED, ORDER_CREATED, _seed_products

pytestmark = pytest.mark.anyio


def stateful_pp():
    """Fake PayPal that remembers the created order's custom_id for later GETs."""
    sent = {}

    def create(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json=ORDER_CREATED)

    def get_order(request):
        return httpx.Response(200, json={"id": "O1", "purchase_units": [{"custom_id": sent["body"]["purchase_units"][0]["custom_id"]}]})

    return FakePayPal({
        ("POST", "/v2/checkout/orders"): create,
        ("POST", "/v2/checkout/orders/O1/capture"): CAPTURED,
        ("GET", "/v2/checkout/orders/O1"): get_order,
    })


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def login(c):
    return {"Authorization": "Bearer " + (await c.post("/api/login", json={"password": "admin-pass"})).json()["token"]}


async def test_login_ok_and_bad(db, settings):
    async with client(create_app(settings, pool=db, paypal=FakePayPal().client)) as c:
        assert (await c.post("/api/login", json={"password": "nope"})).status_code == 401
        ok = await c.post("/api/login", json={"password": "admin-pass"})
        assert ok.status_code == 200 and ok.json()["token"]


async def test_seller_route_needs_token(db, settings):
    async with client(create_app(settings, pool=db, paypal=FakePayPal().client)) as c:
        assert (await c.get("/api/orders")).status_code == 401
        assert (await c.get("/api/orders", headers={"Authorization": "Bearer forged.token"})).status_code == 401
        assert (await c.get("/api/orders", headers=await login(c))).status_code == 200


async def test_mandate_confirm_capture_verify_flow(db, settings):
    await _seed_products(db)
    await dbmod.execute(db, "UPDATE products SET name = %s WHERE sku = %s", "Linen & <Sage>", "THROW-SAGE")
    async with client(create_app(settings, pool=db, paypal=stateful_pp().client)) as c:
        auth = await login(c)
        md = (await c.post("/api/mandates", headers=auth, json={
            "instruction": "Buy one linen throw in sage under $90", "max_amount": "90.00", "currency": "USD",
            "attributes": {"color": "sage"}, "items": [{"sku": "THROW-SAGE", "qty": 1}],
            "agent_id": "agt_test", "agent_platform": "custom"})).json()
        mid = md["mandate_id"]
        summary = (await c.get(f"/api/mandates/{mid}")).json()
        assert summary["cart"]["total"] == "84.00" and summary["status"] == "PENDING_CONFIRMATION"
        conf = await c.post(f"/api/mandates/{mid}/confirm", json={"method": "hosted_page_click"})
        assert conf.json()["approve_url"] == "https://x/approve"
        cap = await c.post("/api/orders/O1/capture")
        assert cap.json()["capture_id"] == "C1"
        verify = (await c.get(f"/api/receipts/{mid}/verify")).json()
        assert verify["strength"] == "STRONG" and verify["custom_id_match"] is True
        orders = (await c.get("/api/orders", headers=auth)).json()
        assert orders[0]["receipt_strength"] == "STRONG"
        page = await c.get(f"/verify/{mid}")
        assert page.status_code == 200 and "STRONG" in page.text
        assert "USD 84.00" in page.text and "Payment captured" in page.text
        assert "Linen &amp; &lt;Sage&gt;" in page.text and "Linen & <Sage>" not in page.text
        assert "Buy one linen throw in sage under $90" not in page.text
        assert "<summary>Technical details</summary>" in page.text


async def test_mandate_violation_422(db, settings):
    await _seed_products(db)
    async with client(create_app(settings, pool=db, paypal=FakePayPal().client)) as c:
        resp = await c.post("/api/mandates", headers=await login(c), json={
            "instruction": "x", "max_amount": "10.00", "currency": "USD", "attributes": {},
            "items": [{"sku": "THROW-SAGE", "qty": 1}], "agent_id": "agt_test", "agent_platform": "custom"})
    assert resp.status_code == 422
    assert resp.json()["violations"][0]["code"] == "OVER_CAP"


async def test_keys_endpoint_matches_signing_key(db, settings):
    async with client(create_app(settings, pool=db, paypal=FakePayPal().client)) as c:
        keys = (await c.get("/.well-known/countersign-keys.json")).json()["keys"]
    assert keys == [{"kid": "k1", "ed25519_pub": r.public_key_b64(r.load_key(settings.merchant_signing_key))}]


async def test_dev_return_404_when_simulator_off(db, settings):
    async with client(create_app(settings, pool=db, paypal=FakePayPal().client)) as c:
        assert (await c.get("/dev/return?token=O1")).status_code == 404


async def test_dev_return_captures_when_simulator_on(db, settings):
    from dataclasses import replace

    from app import mandates as m

    await _seed_products(db)
    pp = stateful_pp()
    md = await m.create_mandate(db, settings, instruction="x", max_amount="90.00", currency="USD", attributes={},
                                items=[{"sku": "THROW-SAGE", "qty": 1}], agent_id="agt_test", agent_platform="custom")
    await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA")
    async with client(create_app(replace(settings, simulator_enabled=True), pool=db, paypal=pp.client)) as c:
        page = await c.get("/dev/return?token=O1")
    assert page.status_code == 200 and "C1" in page.text


async def test_capture_declined_maps_error(db, settings):
    await _seed_products(db)
    declined = httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]})
    pp = FakePayPal({("POST", "/v2/checkout/orders"): ORDER_CREATED, ("POST", "/v2/checkout/orders/O1/capture"): declined})
    async with client(create_app(settings, pool=db, paypal=pp.client)) as c:
        md = (await c.post("/api/mandates", headers=await login(c), json={
            "instruction": "x", "max_amount": "90.00", "currency": "USD", "attributes": {},
            "items": [{"sku": "THROW-SAGE", "qty": 1}], "agent_id": "agt_test", "agent_platform": "custom"})).json()
        await c.post(f"/api/mandates/{md['mandate_id']}/confirm", json={"method": "hosted_page_click"})
        resp = await c.post("/api/orders/O1/capture")
    assert resp.status_code == 422
    assert resp.json()["paypal"]["details"][0]["issue"] == "INSTRUMENT_DECLINED"


async def test_public_confirm_cannot_choose_agent_relayed(db, settings):
    await _seed_products(db)
    async with client(create_app(settings, pool=db, paypal=stateful_pp().client)) as c:
        auth = await login(c)
        md = (await c.post("/api/mandates", headers=auth, json={
            "instruction": "x", "max_amount": "90.00", "currency": "USD", "attributes": {},
            "items": [{"sku": "THROW-SAGE", "qty": 1}], "agent_id": "agt_test", "agent_platform": "custom"})).json()
        url = f"/api/mandates/{md['mandate_id']}/confirm"
        assert (await c.post(url, json={"method": "agent_relayed"})).status_code == 401
        assert (await c.post(url, json={"method": "agent_relayed"}, headers=auth)).status_code == 200
