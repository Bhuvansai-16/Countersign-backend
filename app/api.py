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


@router.get("/.well-known/countersign-keys.json")
async def keys(s=Depends(cs)):
    return {"keys": [{"kid": s.settings.key_id, "ed25519_pub": r.public_key_b64(s.key)}]}


@router.get("/verify/{receipt_id}", response_class=HTMLResponse)
async def verify_page(receipt_id: str, s=Depends(cs)):
    view = await m.receipt_with_verification(s.pool, s.pp, s.settings, receipt_id)
    v, rec = view["verification"], view["receipt"]
    checks = "".join(f"<li>{escape(k)}: <b>{'yes' if v[k] else 'no'}</b></li>"
                     for k in ("chain_ok", "seal_ok", "sig_ok", "custom_id_match"))
    events = "".join(f"<tr><td>{e['seq']}</td><td>{escape(e['type'])}</td><td>{escape(e['at'])}</td>"
                     f"<td><code>{e['hash'][:16]}…</code></td></tr>" for e in view["events"])
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Countersign receipt</title>
<style>body{{font:15px system-ui;max-width:720px;margin:2rem auto;padding:0 16px}}code{{word-break:break-all}}
td{{padding:2px 8px}}.badge{{font-weight:700;padding:2px 8px;border:1px solid}}</style>
<h1>Receipt {escape(receipt_id)}</h1>
<p>Strength: <span class=badge>{escape(v['strength'])}</span> (checked against {escape(v['checked_against'])})</p>
<ul>{checks}</ul>
<p>Seal hash: <code>{escape(rec.get('seal_hash') or '-')}</code><br>
PayPal order: <code>{escape(view['paypal_order_id'] or '-')}</code><br>
PayPal custom_id: <code>{escape(view['paypal_custom_id'] or '-')}</code></p>
<table><tr><th>#</th><th>Event</th><th>At</th><th>Hash</th></tr>{events}</table>
<p>Public key: <a href="/.well-known/countersign-keys.json">countersign-keys.json</a></p>"""


@router.get("/dev/return", response_class=HTMLResponse)
async def dev_return(token: str, s=Depends(cs)):
    if not s.settings.simulator_enabled:
        raise HTTPException(404)
    out = await m.capture(s.pool, s.pp, token)
    return f"<!doctype html><p>Captured order {escape(token)}: capture {escape(out['capture_id'])} ({escape(out['status'])})</p>"
