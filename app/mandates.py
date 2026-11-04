"""Agent mandates -> human confirmation -> PayPal order (custom_id commitment) -> capture -> tracking."""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from psycopg.types.json import Jsonb

from app import db as dbmod
from app import receipts as r

SHIPPING = Decimal("6.00")
MANDATE_TTL = timedelta(hours=24)
METHODS = {"hosted_page_click", "agent_relayed"}


class MandateError(Exception):
    def __init__(self, status: int, detail: str, violations: list | None = None):
        self.status, self.detail, self.violations = status, detail, violations or []
        super().__init__(detail)


def _money(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def validate_cart(products: dict, items: list[dict], max_amount: Decimal, attributes: dict) -> tuple[dict, list[dict]]:
    lines, violations, item_total = [], [], Decimal("0")
    for it in items:
        sku, qty = it.get("sku"), int(it.get("qty", 0))
        p = products.get(sku)
        if p is None:
            violations.append({"code": "UNKNOWN_SKU", "sku": sku})
            continue
        if qty < 1:
            violations.append({"code": "BAD_QTY", "sku": sku, "qty": qty})
            continue
        for k, wanted in attributes.items():
            got = p["attributes"].get(k)
            if got != wanted:
                violations.append({"code": "ATTRIBUTE_MISMATCH", "sku": sku, "attribute": k, "wanted": wanted, "got": got})
        price = Decimal(p["price"])
        item_total += price * qty
        lines.append({"sku": sku, "name": p["name"], "qty": qty, "unit_price": _money(price)})
    total = item_total + SHIPPING
    if total > max_amount:
        violations.append({"code": "OVER_CAP", "total": _money(total), "max_amount": _money(max_amount)})
    quote = {"items": lines, "item_total": _money(item_total), "shipping": _money(SHIPPING), "total": _money(total)}
    return quote, violations


async def _products(pool) -> dict:
    return {p["sku"]: p for p in await dbmod.fetch(pool, "SELECT * FROM products")}


async def create_mandate(pool, settings, *, instruction: str, max_amount: str, currency: str, attributes: dict,
                         items: list[dict], agent_id: str, agent_platform: str,
                         agent_public_key: str | None = None, agent_sig: str | None = None) -> dict:
    try:
        cap = Decimal(str(max_amount))
    except InvalidOperation:
        raise MandateError(422, "max_amount must be a decimal string")
    attributes = attributes or {}
    quote, violations = validate_cart(await _products(pool), items, cap, attributes)
    if violations:
        raise MandateError(422, "cart violates the mandate", violations)

    signed = False
    if agent_sig or agent_public_key:
        body = {"instruction": instruction, "max_amount": str(max_amount), "currency": currency,
                "attributes": attributes, "items": items}
        if not (agent_sig and agent_public_key and r.verify_sig(agent_public_key, r.canonical(body), agent_sig)):
            raise MandateError(401, "agent signature does not verify")
        signed = True

    agent = await dbmod.fetchrow(pool, "SELECT * FROM agents WHERE id = %s", agent_id)
    if agent and agent["public_key"] and agent_public_key and agent["public_key"] != agent_public_key:
        raise MandateError(409, "agent public key differs from the key pinned on first use")
    await dbmod.execute(
        pool,
        "INSERT INTO agents (id, claimed_platform, public_key, last_seen) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (id) DO UPDATE SET last_seen = now(), public_key = COALESCE(agents.public_key, EXCLUDED.public_key)",
        agent_id, agent_platform, agent_public_key,
    )

    rid = "rcpt_" + secrets.token_hex(12)
    expires = datetime.now(timezone.utc) + MANDATE_TTL
    constraints = {"max_amount": _money(cap), "currency": currency, "attributes": attributes, "expires_at": r.iso_z(expires)}
    await dbmod.execute(
        pool,
        "INSERT INTO receipts (id, status, instruction, constraints, cart, agent_id, agent_sig, agent_sig_ok, expires_at) "
        "VALUES (%s, 'PENDING_CONFIRMATION', %s, %s, %s, %s, %s, %s, %s)",
        rid, instruction, Jsonb(constraints), Jsonb(quote), agent_id, agent_sig, signed, expires,
    )
    await r.append_event(pool, rid, "MANDATE_CREATED", {
        "instruction": instruction, "constraints": constraints,
        "agent": {"id": agent_id, "claimed_platform": agent_platform, "signed": signed},
    })
    await r.append_event(pool, rid, "CART_SNAPSHOT", {"cart": quote})
    return {"mandate_id": rid, "quote": quote, "confirm_url": f"{settings.frontend_origin}/confirm/{rid}",
            "expires_at": constraints["expires_at"], "agent_signed": signed}


def _approve_url(order_raw: dict | None) -> str | None:
    for link in (order_raw or {}).get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    return None


async def _order_for(pool, receipt_id: str) -> dict | None:
    return await dbmod.fetchrow(pool, "SELECT * FROM orders WHERE receipt_id = %s", receipt_id)


async def get_mandate(pool, mandate_id: str) -> dict:
    rec = await r.load_receipt(pool, mandate_id)
    if not rec:
        raise MandateError(404, "mandate not found")
    order = await _order_for(pool, mandate_id)
    agent = await dbmod.fetchrow(pool, "SELECT id, claimed_platform FROM agents WHERE id = %s", rec["agent_id"])
    return {
        "mandate_id": rec["id"], "status": rec["status"], "instruction": rec["instruction"],
        "constraints": rec["constraints"], "cart": rec["cart"],
        "agent": {**(agent or {}), "signed": bool(rec["agent_sig_ok"])},
        "expires_at": r.iso_z(rec["expires_at"]),
        "paypal_order_id": order["paypal_order_id"] if order else None,
        "approve_url": _approve_url(order["raw"]) if order else None,
    }


def _order_body(settings, rec: dict, seal_hash: str) -> dict:
    cart, cur = rec["cart"], rec["constraints"]["currency"]
    amt = lambda v: {"currency_code": cur, "value": v}  # noqa: E731
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": rec["id"], "custom_id": r.custom_id_for(seal_hash), "invoice_id": rec["id"],
            "amount": {**amt(cart["total"]), "breakdown": {"item_total": amt(cart["item_total"]), "shipping": amt(cart["shipping"])}},
            "items": [{"name": i["name"][:127], "sku": i["sku"], "quantity": str(i["qty"]),
                       "unit_amount": amt(i["unit_price"]), "category": "PHYSICAL_GOODS"} for i in cart["items"]],
        }],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": f"{settings.public_base_url}/dev/return",
            "cancel_url": f"{settings.public_base_url}/dev/cancel",
            "user_action": "PAY_NOW", "shipping_preference": "GET_FROM_FILE",
            "brand_name": settings.merchant_name[:127],
        }}},
    }


async def confirm_mandate(pool, pp, settings, mandate_id: str, method: str, user_agent: str) -> dict:
    if method not in METHODS:
        raise MandateError(422, f"method must be one of {sorted(METHODS)}")
    rec = await r.load_receipt(pool, mandate_id)
    if not rec:
        raise MandateError(404, "mandate not found")
    if rec["status"] in ("ORDERED", "CAPTURED"):
        order = await _order_for(pool, mandate_id)
        return {"paypal_order_id": order["paypal_order_id"], "approve_url": _approve_url(order["raw"])}
    if rec["status"] == "CANCELLED":
        raise MandateError(409, "mandate was cancelled")
    if rec["status"] == "EXPIRED" or rec["expires_at"] < datetime.now(timezone.utc):
        await dbmod.execute(pool, "UPDATE receipts SET status = 'EXPIRED' WHERE id = %s AND status = 'PENDING_CONFIRMATION'", mandate_id)
        raise MandateError(410, "mandate expired")

    # Gate: only one caller moves PENDING -> CONFIRMED and appends the confirmation event.
    gate = await dbmod.fetchrow(
        pool, "UPDATE receipts SET status = 'CONFIRMED', confirmation_method = %s "
        "WHERE id = %s AND status = 'PENDING_CONFIRMATION' RETURNING id", method, mandate_id)
    if gate:
        if method == "hosted_page_click":
            ua_hash = hashlib.sha256(user_agent.encode()).hexdigest()
            await r.append_event(pool, mandate_id, "BUYER_CONFIRMED", {"method": method, "ua_hash": ua_hash})
        else:
            await r.append_event(pool, mandate_id, "AGENT_RELAYED_CONFIRMATION", {"method": method})
        events = await r.load_events(pool, mandate_id)
        seal = r.seal_hash_of(events)
        key = r.load_key(settings.merchant_signing_key)
        sig = r.sign(key, r.seal_payload(mandate_id, seal, settings.merchant_name, r.iso_z(rec["created_at"])))
        await dbmod.execute(pool, "UPDATE receipts SET seal_hash = %s, merchant_sig = %s, key_id = %s WHERE id = %s",
                            seal, sig, settings.key_id, mandate_id)
    rec = await r.load_receipt(pool, mandate_id)  # CONFIRMED (now or by an earlier call whose order step failed)

    # Deterministic request id: a retry after a failed/unknown response is idempotent at PayPal.
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "countersign-order:" + mandate_id))
    order = await pp.create_order(_order_body(settings, rec, rec["seal_hash"]), request_id)
    custom_id = r.custom_id_for(rec["seal_hash"])
    await dbmod.execute(
        pool,
        "INSERT INTO orders (paypal_order_id, receipt_id, status, amount, currency, custom_id, request_id, raw) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (paypal_order_id) DO NOTHING",
        order["id"], mandate_id, order.get("status", "CREATED"), Decimal(rec["cart"]["total"]),
        rec["constraints"]["currency"], custom_id, request_id, Jsonb(order),
    )
    moved = await dbmod.fetchrow(pool, "UPDATE receipts SET status = 'ORDERED' WHERE id = %s AND status = 'CONFIRMED' RETURNING id", mandate_id)
    if moved:
        await r.append_event(pool, mandate_id, "ORDER_CREATED", {"paypal_order_id": order["id"], "custom_id": custom_id})
    return {"paypal_order_id": order["id"], "approve_url": _approve_url(order)}


async def capture(pool, pp, order_id: str) -> dict:
    row = await dbmod.fetchrow(pool, "SELECT * FROM orders WHERE paypal_order_id = %s", order_id)
    if not row:
        raise MandateError(404, "unknown order")
    if row["capture_id"]:
        return {"status": row["status"], "capture_id": row["capture_id"]}
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "countersign-capture:" + order_id))
    resp = await pp.capture_order(order_id, request_id)
    unit = resp["purchase_units"][0]
    cap = unit["payments"]["captures"][0]
    payer_id = (resp.get("payer") or {}).get("payer_id")
    await dbmod.execute(
        pool,
        "UPDATE orders SET capture_id = %s, status = %s, payer_id = %s, shipping_address = %s, raw = %s "
        "WHERE paypal_order_id = %s",
        cap["id"], resp["status"], payer_id, Jsonb((unit.get("shipping") or {}).get("address")), Jsonb(resp), order_id,
    )
    if row["receipt_id"]:
        payer_hash = hashlib.sha256((payer_id or "").encode()).hexdigest()
        await r.append_event(pool, row["receipt_id"], "PAYPAL_APPROVED", {"payer_id_hash": payer_hash})
        await r.append_event(pool, row["receipt_id"], "CAPTURED", {"capture_id": cap["id"], "amount": cap["amount"]})
        await dbmod.execute(pool, "UPDATE receipts SET status = 'CAPTURED' WHERE id = %s", row["receipt_id"])
    return {"status": resp["status"], "capture_id": cap["id"]}


async def add_tracking(pool, pp, order_id: str, carrier: str, tracking_number: str,
                       status: str = "SHIPPED", notify_payer: bool = False) -> dict:
    row = await dbmod.fetchrow(pool, "SELECT * FROM orders WHERE paypal_order_id = %s", order_id)
    if not row or not row["capture_id"]:
        raise MandateError(409, "order must be captured before tracking")
    body = {"capture_id": row["capture_id"], "tracking_number": tracking_number, "carrier": carrier, "notify_payer": notify_payer}
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"countersign-track:{order_id}:{tracking_number}"))
    resp = await pp.track_order(order_id, body, request_id)
    trackers = (((resp.get("purchase_units") or [{}])[0]).get("shipping") or {}).get("trackers") or []
    tracker_id = trackers[-1]["id"] if trackers else f"{row['capture_id']}-{tracking_number}"
    await dbmod.execute(
        pool, "INSERT INTO shipments (paypal_order_id, tracker_id, carrier, tracking_number, status, raw) "
        "VALUES (%s, %s, %s, %s, 'SHIPPED', %s)", order_id, tracker_id, carrier, tracking_number, Jsonb(resp))
    if row["receipt_id"]:
        await r.append_event(pool, row["receipt_id"], "TRACKING_ADDED",
                             {"carrier": carrier, "tracking_number": tracking_number, "tracker_id": tracker_id})
    if status != "SHIPPED":
        await update_tracking(pool, pp, order_id, status)
    return {"tracker_id": tracker_id}


async def update_tracking(pool, pp, order_id: str, status: str) -> dict:
    ship = await dbmod.fetchrow(pool, "SELECT * FROM shipments WHERE paypal_order_id = %s ORDER BY id DESC LIMIT 1", order_id)
    if not ship:
        raise MandateError(404, "no shipment for order")
    await pp.update_tracker(order_id, ship["tracker_id"], [{"op": "replace", "path": "/status", "value": status}])
    await dbmod.execute(
        pool, "UPDATE shipments SET status = %s, delivered_at = CASE WHEN %s = 'DELIVERED' THEN now() ELSE delivered_at END "
        "WHERE id = %s", status, status, ship["id"])
    order = await dbmod.fetchrow(pool, "SELECT receipt_id FROM orders WHERE paypal_order_id = %s", order_id)
    if order and order["receipt_id"]:
        await r.append_event(pool, order["receipt_id"], "TRACKING_UPDATED", {"tracker_id": ship["tracker_id"], "status": status})
    return {"tracker_id": ship["tracker_id"], "status": status}


async def cancel_mandate(pool, mandate_id: str) -> dict:
    row = await dbmod.fetchrow(pool, "UPDATE receipts SET status = 'CANCELLED' WHERE id = %s "
                               "AND status = 'PENDING_CONFIRMATION' RETURNING status", mandate_id)
    if not row:
        raise MandateError(409, "only a mandate awaiting confirmation can be cancelled")
    return {"mandate_id": mandate_id, "status": "CANCELLED"}


async def mandate_status(pool, mandate_id: str) -> dict:
    rec = await r.load_receipt(pool, mandate_id)
    if not rec:
        raise MandateError(404, "mandate not found")
    order = await _order_for(pool, mandate_id)
    return {"mandate_id": mandate_id, "status": rec["status"],
            "paypal_order_id": order["paypal_order_id"] if order else None,
            "approve_url": _approve_url(order["raw"]) if order else None,
            "capture_id": order["capture_id"] if order else None}


def products_json(rows: list[dict]) -> list[dict]:
    return [{"sku": p["sku"], "name": p["name"], "description": p["description"], "price": _money(Decimal(p["price"])),
             "attributes": p["attributes"], "image_url": p["image_url"]} for p in rows]

