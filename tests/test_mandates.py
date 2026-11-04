import json
from decimal import Decimal

import pytest

from app import db as dbmod
from app import mandates as m
from app import receipts as r
from tests.conftest import FakePayPal

PRODUCTS = {
    "THROW-SAGE": {"sku": "THROW-SAGE", "name": "Linen throw - sage", "price": Decimal("78.00"), "attributes": {"color": "sage"}},
    "THROW-BLUE": {"sku": "THROW-BLUE", "name": "Linen throw - blue", "price": Decimal("78.00"), "attributes": {"color": "blue"}},
}

ORDER_CREATED = {"id": "O1", "status": "PAYER_ACTION_REQUIRED",
                 "links": [{"rel": "self", "href": "https://x/self"}, {"rel": "payer-action", "href": "https://x/approve"}]}
CAPTURED = {
    "id": "O1", "status": "COMPLETED", "payer": {"payer_id": "PAYER1"},
    "purchase_units": [{
        "shipping": {"address": {"address_line_1": "1 Main St", "admin_area_2": "San Jose", "country_code": "US"}},
        "payments": {"captures": [{"id": "C1", "status": "COMPLETED", "amount": {"value": "84.00", "currency_code": "USD"}}]},
    }],
}


def fake_pp():
    return FakePayPal({
        ("POST", "/v2/checkout/orders"): ORDER_CREATED,
        ("POST", "/v2/checkout/orders/O1/capture"): CAPTURED,
        ("POST", "/v2/checkout/orders/O1/track"): {"purchase_units": [{"shipping": {"trackers": [{"id": "C1-9400"}]}}]},
        ("PATCH", "/v2/checkout/orders/O1/trackers/"): {},
    })


# --- pure cart validation ---

def test_quote_total():
    quote, violations = m.validate_cart(PRODUCTS, [{"sku": "THROW-SAGE", "qty": 1}], Decimal("90"), {})
    assert violations == []
    assert quote["total"] == "84.00"
    assert quote["shipping"] == "6.00"
    assert quote["items"][0]["unit_price"] == "78.00"


def test_over_cap():
    _, violations = m.validate_cart(PRODUCTS, [{"sku": "THROW-SAGE", "qty": 1}], Decimal("80"), {})
    assert [v["code"] for v in violations] == ["OVER_CAP"]


def test_attribute_mismatch():
    _, violations = m.validate_cart(PRODUCTS, [{"sku": "THROW-BLUE", "qty": 1}], Decimal("90"), {"color": "sage"})
    assert violations == [{"code": "ATTRIBUTE_MISMATCH", "sku": "THROW-BLUE", "attribute": "color", "wanted": "sage", "got": "blue"}]


def test_unknown_sku_and_bad_qty():
    _, violations = m.validate_cart(PRODUCTS, [{"sku": "NOPE", "qty": 1}, {"sku": "THROW-SAGE", "qty": 0}], Decimal("90"), {})
    assert {v["code"] for v in violations} == {"UNKNOWN_SKU", "BAD_QTY"}


# --- DB + fake PayPal ---

async def _seed_products(pool):
    for p in PRODUCTS.values():
        await dbmod.execute(pool, "INSERT INTO products (sku, name, price, attributes) VALUES (%s, %s, %s, %s)",
                            p["sku"], p["name"], p["price"], json.dumps(p["attributes"]))


async def _mandate(pool, settings, **kw):
    args = dict(instruction="Buy one linen throw in sage under $90", max_amount="90.00", currency="USD",
                attributes={"color": "sage"}, items=[{"sku": "THROW-SAGE", "qty": 1}],
                agent_id="agt_test", agent_platform="custom")
    args.update(kw)
    return await m.create_mandate(pool, settings, **args)


@pytest.mark.anyio
async def test_create_mandate_422_on_violation(db, settings):
    await _seed_products(db)
    with pytest.raises(m.MandateError) as exc:
        await _mandate(db, settings, items=[{"sku": "THROW-BLUE", "qty": 1}])
    assert exc.value.status == 422
    assert exc.value.violations[0]["code"] == "ATTRIBUTE_MISMATCH"


@pytest.mark.anyio
async def test_confirm_commits_custom_id(db, settings):
    await _seed_products(db)
    pp = fake_pp()
    md = await _mandate(db, settings)
    assert md["confirm_url"].endswith("/confirm/" + md["mandate_id"])
    out = await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA/1")
    assert out == {"paypal_order_id": "O1", "approve_url": "https://x/approve"}
    body = json.loads(pp.calls_to("POST", "/v2/checkout/orders")[0].content)
    receipt = await r.load_receipt(db, md["mandate_id"])
    events = await r.load_events(db, md["mandate_id"])
    confirmed = next(e for e in events if e["type"] == "BUYER_CONFIRMED")
    assert receipt["seal_hash"] == confirmed["hash"]
    assert body["purchase_units"][0]["custom_id"] == "cs1:" + receipt["seal_hash"]
    assert body["purchase_units"][0]["amount"]["value"] == "84.00"
    assert [e["type"] for e in events][-1] == "ORDER_CREATED"


@pytest.mark.anyio
async def test_confirm_twice_one_order(db, settings):
    await _seed_products(db)
    pp = fake_pp()
    md = await _mandate(db, settings)
    first = await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA")
    second = await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA")
    assert first == second
    assert len(pp.calls_to("POST", "/v2/checkout/orders")) == 1


@pytest.mark.anyio
async def test_expired_mandate_410(db, settings):
    await _seed_products(db)
    md = await _mandate(db, settings)
    await dbmod.execute(db, "UPDATE receipts SET expires_at = now() - interval '1 minute' WHERE id = %s", md["mandate_id"])
    with pytest.raises(m.MandateError) as exc:
        await m.confirm_mandate(db, fake_pp().client, settings, md["mandate_id"], "hosted_page_click", "UA")
    assert exc.value.status == 410


@pytest.mark.anyio
async def test_capture_appends_events_and_strength_strong(db, settings):
    await _seed_products(db)
    pp = fake_pp()
    md = await _mandate(db, settings)
    await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA")
    out = await m.capture(db, pp.client, "O1")
    assert out == {"status": "COMPLETED", "capture_id": "C1"}
    sent_custom_id = json.loads(pp.calls_to("POST", "/v2/checkout/orders")[0].content)["purchase_units"][0]["custom_id"]
    receipt = await r.load_receipt(db, md["mandate_id"])
    events = await r.load_events(db, md["mandate_id"])
    pub = r.public_key_b64(r.load_key(settings.merchant_signing_key))
    verdict = r.verify_receipt(receipt, events, sent_custom_id, pub, merchant_name=settings.merchant_name)
    assert verdict["strength"] == "STRONG"
    assert receipt["status"] == "CAPTURED"
    # idempotent
    assert await m.capture(db, pp.client, "O1") == out
    assert len(pp.calls_to("POST", "/v2/checkout/orders/O1/capture")) == 1


@pytest.mark.anyio
async def test_agent_key_pinned(db, settings):
    await _seed_products(db)
    k1, k2 = r.load_key(r.new_signing_seed()), r.load_key(r.new_signing_seed())

    def signed(key):
        body = {"instruction": "Buy one linen throw in sage under $90", "max_amount": "90.00", "currency": "USD",
                "attributes": {"color": "sage"}, "items": [{"sku": "THROW-SAGE", "qty": 1}]}
        return {"agent_public_key": r.public_key_b64(key), "agent_sig": r.sign(key, r.canonical(body))}

    first = await _mandate(db, settings, **signed(k1))
    assert first["agent_signed"] is True
    with pytest.raises(m.MandateError) as exc:
        await _mandate(db, settings, **signed(k2))
    assert exc.value.status == 409


@pytest.mark.anyio
async def test_update_tracking_delivered(db, settings):
    await _seed_products(db)
    pp = fake_pp()
    md = await _mandate(db, settings)
    await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA")
    await m.capture(db, pp.client, "O1")
    assert await m.add_tracking(db, pp.client, "O1", "USPS", "9400") == {"tracker_id": "C1-9400"}
    await m.update_tracking(db, pp.client, "O1", "DELIVERED")
    assert len(pp.calls_to("PATCH", "/v2/checkout/orders/O1/trackers/C1-9400")) == 1
    ship = await dbmod.fetchrow(db, "SELECT status, delivered_at FROM shipments")
    assert ship["status"] == "DELIVERED" and ship["delivered_at"] is not None
    events = await r.load_events(db, md["mandate_id"])
    assert [e["type"] for e in events][-2:] == ["TRACKING_ADDED", "TRACKING_UPDATED"]
    assert r.verify_chain(events)
