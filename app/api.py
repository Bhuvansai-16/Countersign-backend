import hmac
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import auth
from app import db as dbmod
from app import mandates as m
from app import receipts as r

router = APIRouter()


def cs(request: Request):
    return request.app.state.cs


class Login(BaseModel):
    password: str


class Item(BaseModel):
    sku: str
    qty: int = 1


class MandateIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    max_amount: str
    currency: str = "USD"
    attributes: dict = {}
    items: list[Item]
    agent_id: str = "seller-console"
    agent_platform: str = "custom"
    agent_public_key: str | None = None
    agent_sig: str | None = None


class ConfirmIn(BaseModel):
    method: str = "hosted_page_click"


class TrackIn(BaseModel):
    carrier: str | None = None
    tracking_number: str | None = None
    status: str | None = None
    notify_payer: bool = False


@router.post("/api/login")
async def login(body: Login, s=Depends(cs)):
    if not hmac.compare_digest(body.password.encode(), s.settings.admin_password.encode()):
        raise HTTPException(401, "wrong password")
    return {"token": auth.make_token(s.settings.session_secret)}


@router.get("/api/products")
async def products(s=Depends(cs)):
    return m.products_json(await dbmod.fetch(s.pool, "SELECT * FROM products ORDER BY sku"))


@router.post("/api/mandates", dependencies=[Depends(auth.require_seller)])
async def create_mandate(body: MandateIn, s=Depends(cs)):
    data = body.model_dump()
    data["items"] = [i for i in data["items"]]
    return await m.create_mandate(s.pool, s.settings, **data)


@router.get("/api/mandates/{mandate_id}")
async def get_mandate(mandate_id: str, s=Depends(cs)):
    return await m.get_mandate(s.pool, mandate_id)


@router.post("/api/mandates/{mandate_id}/confirm")
async def confirm(mandate_id: str, body: ConfirmIn, request: Request, s=Depends(cs)):
    # Public route = the buyer's hosted page. Only the seller (seeding/demo) may record a relayed confirmation.
    # ponytail: a page click is a weak human signal; PayPal's buyer login (PAYPAL_APPROVED) is the strong one.
    # Unforgeable human presence would need a passkey (WebAuthn) on the confirm page.
    if body.method != "hosted_page_click":
        auth.require_seller(request)
    return await m.confirm_mandate(s.pool, s.pp, s.settings, mandate_id, body.method,
                                   request.headers.get("user-agent", ""))


@router.post("/api/orders/{order_id}/capture")
async def capture(order_id: str, s=Depends(cs)):
    return await m.capture(s.pool, s.pp, order_id)


@router.post("/api/orders/{order_id}/track", dependencies=[Depends(auth.require_seller)])
async def track(order_id: str, body: TrackIn, s=Depends(cs)):
    if body.carrier and body.tracking_number:
        return await m.add_tracking(s.pool, s.pp, order_id, body.carrier, body.tracking_number,
                                    body.status or "SHIPPED", body.notify_payer)
    if body.status:
        return await m.update_tracking(s.pool, s.pp, order_id, body.status)
    raise HTTPException(422, "send carrier + tracking_number, or status")


@router.get("/api/orders", dependencies=[Depends(auth.require_seller)])
async def orders(s=Depends(cs)):
    rows = await dbmod.fetch(s.pool, """
        SELECT o.paypal_order_id, o.receipt_id, o.capture_id, o.status, o.amount, o.currency, o.created_at,
               (SELECT status FROM shipments sh WHERE sh.paypal_order_id = o.paypal_order_id ORDER BY id DESC LIMIT 1)
                 AS tracking_status
        FROM orders o ORDER BY o.created_at DESC""")
    out = []
    for row in rows:
        strength = "NONE"
        if row["receipt_id"]:
            view = await m.receipt_with_verification(s.pool, s.pp, s.settings, row["receipt_id"], live=False)
            strength = view["verification"]["strength"]
        out.append({**row, "amount": str(row["amount"]), "created_at": r.iso_z(row["created_at"]),
                    "receipt_strength": strength})
    return out


@router.get("/api/receipts/{receipt_id}", dependencies=[Depends(auth.require_seller)])
async def receipt(receipt_id: str, s=Depends(cs)):
    return await m.receipt_with_verification(s.pool, s.pp, s.settings, receipt_id)


@router.get("/api/receipts/{receipt_id}/verify")
async def verify(receipt_id: str, s=Depends(cs)):
    view = await m.receipt_with_verification(s.pool, s.pp, s.settings, receipt_id)
    return {**view["verification"], "receipt_id": receipt_id, "seal_hash": view["receipt"]["seal_hash"],
            "paypal_order_id": view["paypal_order_id"], "paypal_custom_id": view["paypal_custom_id"]}


@router.get("/api/receipts/{receipt_id}/ap2")
async def receipt_ap2(receipt_id: str, s=Depends(cs)):
    rec = await r.load_receipt(s.pool, receipt_id)
    if not rec:
        raise HTTPException(404, "receipt not found")
    order = await dbmod.fetchrow(s.pool, "SELECT * FROM orders WHERE receipt_id = %s", receipt_id)
    return r.ap2_export(rec, await r.load_events(s.pool, receipt_id), order, s.key, s.settings.key_id, s.settings.merchant_name)


@router.get("/.well-known/countersign-keys.json")
async def keys(s=Depends(cs)):
    return {"keys": [{"kid": s.settings.key_id, "ed25519_pub": r.public_key_b64(s.key)}]}


@router.get("/verify/{receipt_id}", response_class=HTMLResponse)
async def verify_page(receipt_id: str, s=Depends(cs)):
    view = await m.receipt_with_verification(s.pool, s.pp, s.settings, receipt_id)
    v, rec = view["verification"], view["receipt"]
    check_labels = {
        "chain_ok": "Event history is intact",
        "seal_ok": "Receipt seal matches the recorded approval",
        "sig_ok": "Merchant signature is valid",
        "custom_id_match": "PayPal order carries the same receipt seal",
    }
    checks = "".join(
        f"<li>{label}<b class={'pass' if v[key] else 'fail'}>{'Passed' if v[key] else 'Not verified'}</b></li>"
        for key, label in check_labels.items()
    )
    event_labels = {
        "MANDATE_CREATED": "Shopping request recorded",
        "CART_SNAPSHOT": "Cart recorded",
        "PRODUCT_DISCOVERY": "Source listing recorded",
        "BUYER_CONFIRMED": "Buyer confirmed the purchase",
        "ORDER_CREATED": "PayPal order created",
        "PAYPAL_APPROVED": "Buyer approved in PayPal",
        "CAPTURED": "Payment captured",
    }
    events = "".join(
        f"<li><span>{escape(event_labels.get(e['type'], e['type'].replace('_', ' ').capitalize()))}</span>"
        f"<time datetime=\"{escape(e['at'])}\">{escape(e['at'].replace('T', ' ').removesuffix('Z'))} UTC</time></li>"
        for e in view["events"]
    )
    hashes = "".join(
        f"<li>{escape(str(e['seq']))} · {escape(e['type'])} <code>{escape(e['hash'])}</code></li>"
        for e in view["events"]
    )
    cart = rec["cart"]
    currency = escape(rec["constraints"]["currency"])
    items = "".join(
        f"<li>{escape(item['name'])}<span>× {escape(str(item['qty']))}</span></li>"
        for item in cart["items"]
    )
    captured_at = next((e["at"] for e in reversed(view["events"]) if e["type"] == "CAPTURED"), None)
    recorded_at = captured_at or rec["created_at"]
    date_label = "Capture recorded" if captured_at else "Receipt created"
    date_value = escape(recorded_at.replace("T", " ").removesuffix("Z"))
    paypal_order = escape(view["paypal_order_id"] or "Pending")
    checked_against = "Checked against PayPal." if view["paypal_order_id"] else "PayPal order pending."
    return f"""<!doctype html><html lang="en"><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Countersign receipt</title>
<style>
  * {{ box-sizing: border-box }}
  body {{ margin: 0; background: #f5f3eb; color: #252d25; font: 15px/1.55 system-ui, sans-serif }}
  main {{ max-width: 1600px; margin: auto; padding: 32px }}
  h1, h2 {{ font: 400 28px/1.2 Georgia, serif; margin: 0 0 18px }}
  h1 {{ font-size: 38px; margin-bottom: 8px }}
  .reference, dt, time {{ color: #626b5e }}
  .reference {{ margin: 0 0 24px }}
  .grid {{ display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 20px }}
  section, details {{ background: #fffef9; border: 1px solid #dcded1; border-radius: 18px; padding: 24px }}
  section ul, ol {{ padding: 0; margin: 0; list-style: none }}
  .rows li {{ display: flex; justify-content: space-between; gap: 16px; padding: 11px 0; border-bottom: 1px solid #dcded1 }}
  .rows li:last-child {{ border-bottom: 0 }}
  .rows li, time {{ overflow-wrap: anywhere }}
  .total {{ display: flex; justify-content: space-between; gap: 16px; font-size: 19px; margin: 18px 0 }}
  dl {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin: 0 }}
  dt {{ font-size: 13px }}
  dd {{ margin: 4px 0 0 }}
  .pass {{ color: #31553d }}
  .fail {{ color: #9a3e22 }}
  .badge {{ display: inline-block; background: #e8eddf; padding: 3px 9px; border-radius: 6px; font-weight: 700 }}
  time {{ font-size: 13px }}
  .activity {{ grid-column: 1 / -1 }}
  details {{ margin-top: 20px }}
  summary {{ cursor: pointer; font-weight: 600 }}
  code {{ overflow-wrap: anywhere }}
  a {{ color: #31553d }}
  @media (max-width: 760px) {{
    main {{ padding: 20px 16px }}
    .grid {{ grid-template-columns: 1fr }}
    section, details {{ padding: 18px }}
    .timeline li {{ flex-wrap: wrap; gap: 3px 16px }}
  }}
</style>
<main>
  <h1>Purchase receipt</h1>
  <p class="reference"><code>{escape(receipt_id)}</code></p>
  <div class="grid">
    <section>
      <h2>Order summary</h2>
      <ul class="rows">{items}<li>Shipping <span>{currency} {escape(cart['shipping'])}</span></li></ul>
      <p class="total">Total <strong>{currency} {escape(cart['total'])}</strong></p>
      <dl>
        <div><dt>PayPal order</dt><dd><code>{paypal_order}</code></dd></div>
        <div><dt>{date_label}</dt><dd><time datetime="{escape(recorded_at)}">{date_value} UTC</time></dd></div>
      </dl>
    </section>
    <section>
      <h2>Verification</h2>
      <p><span class="badge">{escape(v['strength'])}</span> {checked_against}</p>
      <ul class="rows">{checks}</ul>
    </section>
    <section class="activity">
      <h2>Activity timeline</h2>
      <ol class="rows timeline">{events}</ol>
    </section>
  </div>
  <details>
    <summary>Technical details</summary>
    <p>Seal hash <code>{escape(rec.get('seal_hash') or '-')}</code></p>
    <p>PayPal custom_id <code>{escape(view['paypal_custom_id'] or '-')}</code></p>
    <h2>Event hashes</h2>
    <ol>{hashes}</ol>
    <p><a href="/.well-known/countersign-keys.json">Public verification key</a></p>
  </details>
</main></html>"""


@router.get("/dev/return", response_class=HTMLResponse)
async def dev_return(token: str, s=Depends(cs)):
    if not s.settings.simulator_enabled:
        raise HTTPException(404)
    out = await m.capture(s.pool, s.pp, token)
    return f"<!doctype html><p>Captured order {escape(token)}: capture {escape(out['capture_id'])} ({escape(out['status'])})</p>"
