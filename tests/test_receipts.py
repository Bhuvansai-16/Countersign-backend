import base64
import json
from datetime import datetime, timezone

import pytest

from app import db as dbmod
from app import receipts as r

MERCHANT = "Maya's Loom (sandbox)"


def _chain(types, payloads=None):
    events, prev = [], r.GENESIS
    for i, t in enumerate(types):
        payload = (payloads or {}).get(t, {"i": i})
        at = f"2026-11-04T10:00:0{i}Z"
        h = r.event_hash(prev, i, t, at, payload)
        events.append({"seq": i, "type": t, "at": at, "payload": payload, "prev_hash": prev, "hash": h})
        prev = h
    return events


def _signed_receipt(events, key):
    seal = r.seal_hash_of(events)
    created = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    sig = r.sign(key, r.seal_payload("rcpt_1", seal, MERCHANT, r.iso_z(created)))
    return {"id": "rcpt_1", "seal_hash": seal, "merchant_sig": sig, "created_at": created}


HOSTED = {"BUYER_CONFIRMED": {"method": "hosted_page_click"}}
FULL = ["MANDATE_CREATED", "CART_SNAPSHOT", "BUYER_CONFIRMED", "ORDER_CREATED", "PAYPAL_APPROVED", "CAPTURED"]


def test_canonical_sorted_compact():
    assert r.canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_chain_verifies():
    assert r.verify_chain(_chain(FULL)) is True


def test_tamper_payload_fails():
    events = _chain(FULL)
    events[1]["payload"] = {"i": 99}
    assert r.verify_chain(events) is False


def test_tamper_reorder_fails():
    events = _chain(FULL)
    events[1], events[2] = events[2], events[1]
    assert r.verify_chain(events) is False


def test_seal_is_last_pre_seal_event():
    events = _chain(["MANDATE_CREATED", "CART_SNAPSHOT", "BUYER_CONFIRMED", "ORDER_CREATED"])
    assert r.seal_hash_of(events) == events[2]["hash"]


def test_sign_verify_and_bad_sig():
    key, other = r.load_key(r.new_signing_seed()), r.load_key(r.new_signing_seed())
    sig = r.sign(key, b"hello")
    assert r.verify_sig(r.public_key_b64(key), b"hello", sig) is True
    assert r.verify_sig(r.public_key_b64(key), b"hellp", sig) is False
    assert r.verify_sig(r.public_key_b64(other), b"hello", sig) is False


def test_custom_id_len():
    assert len(r.custom_id_for("f" * 64)) == 68


def test_strength_cases():
    hosted = _chain(FULL, HOSTED)
    assert r.strength(hosted, sig_ok=True, custom_id_match=True) == "STRONG"
    assert r.strength(hosted, sig_ok=True, custom_id_match=False) == "WEAK"
    relayed = _chain(["MANDATE_CREATED", "CART_SNAPSHOT", "AGENT_RELAYED_CONFIRMATION", "ORDER_CREATED", "PAYPAL_APPROVED"])
    assert r.strength(relayed, sig_ok=True, custom_id_match=True) == "MEDIUM"
    unapproved = _chain(["MANDATE_CREATED", "CART_SNAPSHOT", "BUYER_CONFIRMED", "ORDER_CREATED"], HOSTED)
    assert r.strength(unapproved, sig_ok=True, custom_id_match=True) == "WEAK"


def test_verify_receipt_strong_and_custom_id_mismatch():
    key = r.load_key(r.new_signing_seed())
    events = _chain(FULL, HOSTED)
    receipt = _signed_receipt(events, key)
    good = r.verify_receipt(receipt, events, r.custom_id_for(receipt["seal_hash"]), r.public_key_b64(key), merchant_name=MERCHANT)
    assert good == {"chain_ok": True, "seal_ok": True, "sig_ok": True, "custom_id_match": True, "strength": "STRONG"}
    bad = r.verify_receipt(receipt, events, "cs1:" + "0" * 64, r.public_key_b64(key), merchant_name=MERCHANT)
    assert bad["custom_id_match"] is False
    assert bad["strength"] == "WEAK"


def test_verify_receipt_tampered_chain_is_weak():
    key = r.load_key(r.new_signing_seed())
    events = _chain(FULL, HOSTED)
    receipt = _signed_receipt(events, key)
    events[0]["payload"] = {"instruction": "something else"}
    out = r.verify_receipt(receipt, events, r.custom_id_for(receipt["seal_hash"]), r.public_key_b64(key), merchant_name=MERCHANT)
    assert out["chain_ok"] is False
    assert out["strength"] == "WEAK"


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jws_verifies():
    key = r.load_key(r.new_signing_seed())
    token = r.jws_eddsa(key, {"cart": "x"}, "k1")
    header_b64, payload_b64, sig_b64 = token.split(".")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    raw_sig = base64.b64encode(_b64url_decode(sig_b64)).decode()
    assert r.verify_sig(r.public_key_b64(key), signing_input, raw_sig) is True
    assert json.loads(_b64url_decode(header_b64))["alg"] == "EdDSA"
    assert json.loads(_b64url_decode(payload_b64)) == {"cart": "x"}


@pytest.mark.anyio
async def test_append_event_chains(db):
    await dbmod.execute(
        db,
        "INSERT INTO receipts (id, status, instruction, constraints, cart, expires_at) "
        "VALUES ('rcpt_1', 'PENDING_CONFIRMATION', 'buy', '{}', '{}', now())",
    )
    for t in ("MANDATE_CREATED", "CART_SNAPSHOT", "BUYER_CONFIRMED"):
        await r.append_event(db, "rcpt_1", t, {"t": t})
    events = await r.load_events(db, "rcpt_1")
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert r.verify_chain(events) is True
    assert (await r.load_receipt(db, "rcpt_1"))["id"] == "rcpt_1"
