"""Intent receipts: canonical JSON, hash chain, seal, Ed25519 signatures, strength, JWS."""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from psycopg.types.json import Jsonb

GENESIS = "0" * 64
PRE_SEAL = {"MANDATE_CREATED", "CART_SNAPSHOT", "BUYER_CONFIRMED", "AGENT_RELAYED_CONFIRMATION"}


def canonical(x) -> bytes:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def iso_z(dt: datetime | str) -> str:
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_z() -> str:
    return iso_z(datetime.now(timezone.utc))


def event_hash(prev_hash: str, seq: int, type: str, at: str, payload: dict) -> str:
    body = canonical({"seq": seq, "type": type, "at": at, "payload": payload})
    return hashlib.sha256(prev_hash.encode() + body).hexdigest()


def verify_chain(events: list[dict]) -> bool:
    prev = GENESIS
    for i, e in enumerate(events):
        if e["seq"] != i or e["prev_hash"] != prev:
            return False
        if event_hash(prev, e["seq"], e["type"], e["at"], e["payload"]) != e["hash"]:
            return False
        prev = e["hash"]
    return True


def seal_hash_of(events: list[dict]) -> str | None:
    pre = [e for e in events if e["type"] in PRE_SEAL]
    return pre[-1]["hash"] if pre else None


def seal_payload(receipt_id: str, seal_hash: str, merchant_name: str, created_at: str) -> bytes:
    return canonical({"v": "cs1", "receipt_id": receipt_id, "seal_hash": seal_hash,
                      "merchant": merchant_name, "created_at": created_at})


def new_signing_seed() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def load_key(seed_b64: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(key: Ed25519PrivateKey, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode()


def verify_sig(public_key_b64_: str, data: bytes, sig_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64_))
        pub.verify(base64.b64decode(sig_b64), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def custom_id_for(seal_hash: str) -> str:
    return "cs1:" + seal_hash


def strength(events: list[dict], sig_ok: bool, custom_id_match: bool) -> str:
    types = {e["type"] for e in events}
    hosted = any(e["type"] == "BUYER_CONFIRMED" and e["payload"].get("method") == "hosted_page_click" for e in events)
    approved = "PAYPAL_APPROVED" in types
    if hosted and approved and sig_ok and custom_id_match:
        return "STRONG"
    if approved and "AGENT_RELAYED_CONFIRMATION" in types and sig_ok:
        return "MEDIUM"
    return "WEAK"


def verify_receipt(receipt: dict, events: list[dict], paypal_custom_id: str | None, public_key: str,
                   *, merchant_name: str) -> dict:
    chain_ok = verify_chain(events)
    seal = receipt.get("seal_hash")
    seal_ok = bool(seal) and seal == seal_hash_of(events)
    sig_ok = seal_ok and bool(receipt.get("merchant_sig")) and verify_sig(
        public_key, seal_payload(receipt["id"], seal, merchant_name, iso_z(receipt["created_at"])), receipt["merchant_sig"]
    )
    custom_id_match = seal_ok and paypal_custom_id == custom_id_for(seal)
    grade = strength(events, sig_ok, custom_id_match) if chain_ok and seal_ok else "WEAK"
    return {"chain_ok": chain_ok, "seal_ok": seal_ok, "sig_ok": sig_ok,
            "custom_id_match": custom_id_match, "strength": grade}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jws_eddsa(key: Ed25519PrivateKey, payload: dict, kid: str) -> str:
    signing_input = _b64url(canonical({"alg": "EdDSA", "typ": "JWT", "kid": kid})) + "." + _b64url(canonical(payload))
    return signing_input + "." + _b64url(key.sign(signing_input.encode("ascii")))


def ap2_export(receipt: dict, events: list[dict], order: dict | None, key: Ed25519PrivateKey, kid: str,
               merchant_name: str) -> dict:
    """The receipt as AP2 mandates. Field names follow google-agentic-commerce/AP2
    code/sdk/python/ap2/models/mandate.py: IntentMandate{user_cart_confirmation_required,
    natural_language_description, merchants, skus, requires_refundability, intent_expiry};
    CartMandate{contents: CartContents{id, user_cart_confirmation_required, payment_request (W3C
    PaymentRequest), cart_expiry, merchant_name}, merchant_authorization (JWT with cart_hash)};
    PaymentMandate{payment_mandate_contents{payment_mandate_id, payment_details_id, payment_details_total,
    payment_response, merchant_agent, timestamp}, user_authorization}."""
    cons, cart = receipt["constraints"], receipt["cart"]
    cur = cons["currency"]
    item = lambda label, value: {"label": label, "amount": {"currency": cur, "value": float(value)}}  # noqa: E731
    intent = {
        "user_cart_confirmation_required": True,
        "natural_language_description": receipt["instruction"],
        "merchants": [merchant_name],
        "skus": [i["sku"] for i in cart["items"]],
        "requires_refundability": False,
        "intent_expiry": cons["expires_at"],
    }
    contents = {
        "id": receipt["id"],
        "user_cart_confirmation_required": True,
        "payment_request": {
            "method_data": [{"supported_methods": "paypal", "data": {"custom_id": custom_id_for(receipt["seal_hash"])
                                                                     if receipt.get("seal_hash") else None}}],
            "details": {
                "id": receipt["id"],
                "display_items": [item(f'{i["name"]} x{i["qty"]}', Decimal(i["unit_price"]) * i["qty"]) for i in cart["items"]]
                                 + [item("Shipping", cart["shipping"])],
                "total": item("Total", cart["total"]),
            },
        },
        "cart_expiry": cons["expires_at"],
        "merchant_name": merchant_name,
    }
    iat = iso_z(receipt["created_at"])
    claims = {"iss": merchant_name, "sub": receipt["id"], "aud": "paypal", "iat": iat, "exp": cons["expires_at"],
              "jti": receipt["id"], "cart_hash": hashlib.sha256(canonical(contents)).hexdigest(),
              "seal_hash": receipt.get("seal_hash")}
    payment = None
    if order:
        payment = {
            "payment_mandate_contents": {
                "payment_mandate_id": order["paypal_order_id"],
                "payment_details_id": receipt["id"],
                "payment_details_total": item("Total", order["amount"]),
                "payment_response": {"request_id": receipt["id"], "method_name": "paypal",
                                     "details": {"paypal_order_id": order["paypal_order_id"], "capture_id": order.get("capture_id")}},
                "merchant_agent": "countersign",
                "timestamp": iso_z(order["created_at"]),
            },
            # The buyer authorised payment by logging in to PayPal; that signature lives with PayPal, not here.
            "user_authorization": None,
        }
    return {"intent_mandate": intent, "cart_mandate": {"contents": contents, "merchant_authorization": jws_eddsa(key, claims, kid)},
            "payment_mandate": payment}


# --- DB helpers -------------------------------------------------------------

async def append_event(pool, receipt_id: str, type: str, payload: dict) -> dict:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT id FROM receipts WHERE id = %s FOR UPDATE", (receipt_id,))
        cur = await conn.execute(
            "SELECT seq, hash FROM receipt_events WHERE receipt_id = %s ORDER BY seq DESC LIMIT 1", (receipt_id,)
        )
        last = await cur.fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (0, GENESIS)
        at = now_z()
        event = {"seq": seq, "type": type, "at": at, "payload": payload, "prev_hash": prev,
                 "hash": event_hash(prev, seq, type, at, payload)}
        await conn.execute(
            "INSERT INTO receipt_events (receipt_id, seq, type, payload, at, prev_hash, hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (receipt_id, seq, type, Jsonb(payload), at, prev, event["hash"]),
        )
        return event


async def load_events(pool, receipt_id: str) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT seq, type, at, payload, prev_hash, hash FROM receipt_events WHERE receipt_id = %s ORDER BY seq",
            (receipt_id,),
        )
        return await cur.fetchall()


async def load_receipt(pool, receipt_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM receipts WHERE id = %s", (receipt_id,))
        return await cur.fetchone()
