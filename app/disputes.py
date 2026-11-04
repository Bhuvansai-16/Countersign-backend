"""Dispute core: ingest, triage, evidence, playbooks, self-check, approval-gated submit. No LLM here."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app import db as dbmod
from app import receipts as r
from app.economics import CaseInputs, decide, normalize_reason
from app.paypal import PayPalError

PLAYBOOKS = Path(__file__).resolve().parents[1] / "corpus" / "playbooks"
ACTION_REL = {"PROVIDE_EVIDENCE": "provide_evidence", "MAKE_OFFER": "make_offer", "ACCEPT_CLAIM": "accept_claim"}
DECISION_ACTION = {"FIGHT": "PROVIDE_EVIDENCE", "OFFER": "MAKE_OFFER", "ACCEPT": "ACCEPT_CLAIM"}
REQUIRED_EVIDENCE = {"INR": ["TRACKING"], "SNAD": ["RECEIPT|BUYER_MESSAGES"], "UNAUTHORIZED": ["RECEIPT"],
                     "CREDIT_NOT_PROCESSED": ["REFUND"]}


class ApprovalError(Exception):
    pass


class Section(BaseModel):
    heading: str
    text: str
    refs: list[str] = []


class Offer(BaseModel):
    offer_type: str
    amount: str


class Draft(BaseModel):
    action: Literal["PROVIDE_EVIDENCE", "MAKE_OFFER", "ACCEPT_CLAIM"]
    evidence_type: str | None = None
    note: str = Field(max_length=2000)
    sections: list[Section] = []
    offer: Offer | None = None


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


# --- ingest / triage ----------------------------------------------------------------------------

async def ingest(pool, pp, dispute_id: str, settings) -> dict:
    d = await pp.get_dispute(dispute_id)
    txn = (d.get("disputed_transactions") or [{}])[0]
    capture_id = txn.get("seller_transaction_id")
    order = await dbmod.fetchrow(pool, "SELECT * FROM orders WHERE capture_id = %s", capture_id) if capture_id else None
    receipt = await r.load_receipt(pool, order["receipt_id"]) if order and order["receipt_id"] else None
    events = await r.load_events(pool, receipt["id"]) if receipt else []
    shipments = await dbmod.fetch(pool, "SELECT * FROM shipments WHERE paypal_order_id = %s ORDER BY id",
                                  order["paypal_order_id"]) if order else []
    amount = (d.get("dispute_amount") or {})
    await dbmod.execute(
        pool,
        "INSERT INTO disputes (dispute_id, capture_id, paypal_order_id, reason, status, stage, amount, currency, "
        "created_at_pp, due_at, raw, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
        "ON CONFLICT (dispute_id) DO UPDATE SET status = EXCLUDED.status, stage = EXCLUDED.stage, "
        "raw = EXCLUDED.raw, updated_at = now()",
        dispute_id, capture_id, order["paypal_order_id"] if order else None, d.get("reason"), d.get("status"),
        d.get("dispute_life_cycle_stage"), Decimal(amount.get("value", "0")), amount.get("currency_code", "USD"),
        _ts(d.get("create_time")), _ts(d.get("seller_response_due_date")), Jsonb(d),
    )
    return {"dispute": d, "order": order, "receipt": receipt, "events": events, "shipments": shipments}


def _capture(order: dict | None) -> dict:
    try:
        return order["raw"]["purchase_units"][0]["payments"]["captures"][0]
    except (TypeError, KeyError, IndexError):
        return {}


async def _live_custom_id(pp, order: dict) -> str | None:
    try:
        o = await pp.get_order(order["paypal_order_id"])
        return (o.get("purchase_units") or [{}])[0].get("custom_id")
    except PayPalError:
        return order["custom_id"]  # ponytail: fall back to our stored copy if PayPal is unreachable


async def _verify(pool, pp, ctx: dict, settings) -> dict | None:
    if not ctx["receipt"]:
        return None
    pub = r.public_key_b64(r.load_key(settings.merchant_signing_key))
    custom_id = await _live_custom_id(pp, ctx["order"])
    return r.verify_receipt(ctx["receipt"], ctx["events"], custom_id, pub, merchant_name=settings.merchant_name)


async def _attribute_check(pool, receipt: dict) -> tuple[bool, bool]:
    """(violation, constraints_match_cart) for a receipt's constraints vs its cart."""
    wanted = receipt["constraints"].get("attributes") or {}
    over_cap = Decimal(receipt["cart"]["total"]) > Decimal(receipt["constraints"]["max_amount"])
    mismatch = False
    for item in receipt["cart"]["items"]:
        p = await dbmod.fetchrow(pool, "SELECT attributes FROM products WHERE sku = %s", item["sku"])
        attrs = p["attributes"] if p else {}
        mismatch |= any(attrs.get(k) != v for k, v in wanted.items())
    return over_cap or mismatch, bool(wanted) and not mismatch


async def _refunded(pool, pp, order: dict | None) -> Decimal:
    if not order or not order["capture_id"]:
        return Decimal(0)
    total = Decimal(0)
    rows = await dbmod.fetch(pool, "SELECT resolution FROM issues WHERE capture_id = %s AND state = 'RESOLVED'", order["capture_id"])
    for row in rows:
        total += Decimal((row["resolution"] or {}).get("amount") or 0)
    try:
        if (await pp.get_capture(order["capture_id"])).get("status") == "REFUNDED":
            total = max(total, Decimal(order["amount"]))
    except PayPalError:
        pass
    return total


async def case_inputs(pool, pp, ctx: dict, settings, now: datetime, verification: dict | None = None) -> CaseInputs:
    d, order, receipt = ctx["dispute"], ctx["order"], ctx["receipt"]
    reason = normalize_reason(d.get("reason"))
    amount = Decimal((d.get("dispute_amount") or {}).get("value", "0"))
    violation, matches = await _attribute_check(pool, receipt) if receipt else (False, False)
    distinct_dup = False
    if reason == "DUPLICATE" and order and order["payer_id"]:
        other = await dbmod.fetchrow(pool, "SELECT count(*) AS n FROM orders WHERE payer_id = %s AND paypal_order_id <> %s "
                                     "AND receipt_id IS DISTINCT FROM %s", order["payer_id"], order["paypal_order_id"], order["receipt_id"])
        distinct_dup = other["n"] > 0
    since = now - timedelta(days=90)
    counts = await dbmod.fetchrow(
        pool,
        "SELECT (SELECT count(*) FROM orders WHERE capture_id IS NOT NULL AND created_at >= %s) "
        "     + (SELECT count(*) FROM history_txns WHERE at >= %s) AS txns, "
        "       (SELECT count(*) FROM disputes WHERE created_at_pp >= %s) "
        "     + (SELECT count(*) FROM history_txns WHERE at >= %s AND disputed) AS disputes",
        since, since, since, since)
    return CaseInputs(
        reason=reason, amount=amount, created_at=_ts(d.get("create_time")) or now,
        due_date=_ts(d.get("seller_response_due_date")), now=now,
        tracking_delivered=any(s["status"] == "DELIVERED" for s in ctx["shipments"]),
        has_shipping_address=bool(order and order["shipping_address"]),
        capture_sp_status=(_capture(order).get("seller_protection") or {}).get("status"),
        receipt_strength=verification["strength"] if verification else "NONE",
        receipt_violation=violation, constraints_match_cart=matches,
        refund_covers_amount=amount > 0 and await _refunded(pool, pp, order) >= amount,
        distinct_duplicate=distinct_dup, disputes_90d=counts["disputes"], txns_90d=counts["txns"],
    )


async def triage(pool, pp, dispute_id: str, settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ctx = await ingest(pool, pp, dispute_id, settings)
    verification = await _verify(pool, pp, ctx, settings)
    econ = decide(await case_inputs(pool, pp, ctx, settings, now, verification))
    d = ctx["dispute"]
    tri = {"reason": normalize_reason(d.get("reason")), "paypal_reason": d.get("reason"),
           "stage": d.get("dispute_life_cycle_stage"), "status": d.get("status"),
           "amount": (d.get("dispute_amount") or {}).get("value"),
           "currency": (d.get("dispute_amount") or {}).get("currency_code"),
           **{k: econ[k] for k in ("due_at", "hours_left", "fee", "sp_eligible", "high_volume", "hv_warning", "dispute_rate_90d")}}
    strength = verification["strength"] if verification else "NONE"
    initial = "EXPIRED" if econ["decision"] == "EXPIRED" else "NEW"
    await dbmod.execute(pool, "UPDATE disputes SET due_at = %s WHERE dispute_id = %s", _ts(econ["due_at"]), dispute_id)
    return await dbmod.fetchrow(
        pool,
        "INSERT INTO cases (dispute_id, state, triage, economics, receipt_strength) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (dispute_id) DO UPDATE SET triage = EXCLUDED.triage, economics = EXCLUDED.economics, "
        "receipt_strength = EXCLUDED.receipt_strength, updated_at = now() RETURNING *",
        dispute_id, initial, Jsonb(tri), Jsonb(econ), strength)


# --- evidence -------------------------------------------------------------------------------------

async def gather_evidence(pool, pp, dispute_id: str, settings) -> tuple[list[dict], list[str]]:
    ctx = await ingest(pool, pp, dispute_id, settings)
    d, order, receipt = ctx["dispute"], ctx["order"], ctx["receipt"]
    items: list[dict] = []

    def add(kind: str, source: str, summary: str, data):
        items.append({"id": f"E{len(items) + 1}", "kind": kind, "source": source, "summary": summary, "data": data})

    amt = d.get("dispute_amount") or {}
    add("DISPUTE", "paypal:disputes", f"Dispute {dispute_id} ({d.get('reason')}) for {amt.get('value')} {amt.get('currency_code')}",
        {k: d.get(k) for k in ("dispute_id", "reason", "status", "dispute_life_cycle_stage", "create_time",
                                "seller_response_due_date", "dispute_amount")})
    if order:
        add("ORDER", "paypal:orders", f"PayPal order {order['paypal_order_id']} for {order['amount']} {order['currency']}",
            {"paypal_order_id": order["paypal_order_id"], "amount": str(order["amount"]), "currency": order["currency"],
             "status": order["status"], "created_at": r.iso_z(order["created_at"]), "shipping_address": order["shipping_address"],
             "custom_id": order["custom_id"]})
        cap = _capture(order)
        if cap:
            add("CAPTURE", "paypal:payments", f"Capture {cap.get('id')} {cap.get('status')}",
                {"capture_id": cap.get("id"), "status": cap.get("status"), "amount": cap.get("amount"),
                 "seller_protection": cap.get("seller_protection"), "create_time": cap.get("create_time")})
    for s in ctx["shipments"]:
        add("TRACKING", "paypal:trackers", f"{s['carrier']} {s['tracking_number']} {s['status']}",
            {"carrier": s["carrier"], "tracking_number": s["tracking_number"], "status": s["status"],
             "delivered_at": r.iso_z(s["delivered_at"]) if s["delivered_at"] else None})
    if receipt:
        add("RECEIPT", "countersign:receipt", f"Intent receipt {receipt['id']} ({receipt['confirmation_method']})",
            {"receipt_id": receipt["id"], "instruction": receipt["instruction"], "constraints": receipt["constraints"],
             "cart": receipt["cart"], "agent_id": receipt["agent_id"], "agent_signed": bool(receipt["agent_sig_ok"]),
             "confirmation_method": receipt["confirmation_method"], "seal_hash": receipt["seal_hash"],
             "events": [{"seq": e["seq"], "type": e["type"], "at": e["at"], "hash": e["hash"]} for e in ctx["events"]]})
        v = await _verify(pool, pp, ctx, settings)
        add("RECEIPT_VERIFICATION", "countersign:verify", f"Receipt strength {v['strength']}", v)
    if d.get("messages"):
        add("BUYER_MESSAGES", "paypal:disputes", f"{len(d['messages'])} message(s) on the dispute",
            [{k: msg.get(k) for k in ("posted_by", "time_posted", "content")} for msg in d["messages"]])
    refunded = await _refunded(pool, pp, order)
    if refunded:
        add("REFUND", "paypal:payments", f"Refunded {refunded} on capture {order['capture_id']}",
            {"capture_id": order["capture_id"], "refunded_total": str(refunded)})
    if receipt:
        for iss in await dbmod.fetch(pool, "SELECT id, type, state, proposal, resolution, created_at FROM issues WHERE receipt_id = %s", receipt["id"]):
            add("ISSUE", "countersign:issues", f"Issue {iss['id']} {iss['type']} {iss['state']}",
                {**iss, "created_at": r.iso_z(iss["created_at"])})

    kinds = {i["kind"] for i in items}
    reason = normalize_reason(d.get("reason"))
    gaps = [req for req in REQUIRED_EVIDENCE.get(reason, []) if not any(k in kinds for k in req.split("|"))]
    await dbmod.execute(pool, "UPDATE cases SET evidence = %s, evidence_gaps = %s, updated_at = now() WHERE dispute_id = %s",
                        Jsonb(items), Jsonb(gaps), dispute_id)
    return items, gaps


# --- playbooks and self-check --------------------------------------------------------------------

POLICY_RE = re.compile(r"^\[(POL-[A-Z]+-\d+)\]", re.MULTILINE)


def load_playbook(reason: str, has_receipt: bool) -> dict:
    name = reason if (PLAYBOOKS / f"{reason}.md").exists() else "OTHER"
    text = (PLAYBOOKS / f"{name}.md").read_text(encoding="utf-8")
    if has_receipt:
        text += "\n\n" + (PLAYBOOKS / "AGENT_INTENT.md").read_text(encoding="utf-8")
    return {"text": text, "policy_ids": set(POLICY_RE.findall(text))}


def policy_lines(text: str, ids: set[str]) -> list[dict]:
    """The cited [POL-...] lines and their sources, for the evidence PDF."""
    out = []
    for block in text.split("\n\n"):
        m_ = POLICY_RE.match(block.strip())
        if m_ and m_.group(1) in ids:
            line, _, src = block.strip().partition("\nSource: ")
            out.append({"id": m_.group(1), "text": line[len(m_.group(0)):].strip(), "source": src.strip()})
    return out


AMOUNT_RE = re.compile(r"\$?\d+\.\d{2}\b")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ID_RE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9-]{8,}\b")


def check_draft(draft: Draft, evidence: list[dict], policy_ids: set[str], economics: dict) -> list[str]:
    """Problems with a draft; empty means it may go to the seller. economics = effective decision (override applied)."""
    corpus = json.dumps([e["data"] for e in evidence] + [e["summary"] for e in evidence], default=str)
    evidence_ids = {e["id"] for e in evidence}
    text = draft.note + " " + " ".join(f"{s.heading} {s.text}" for s in draft.sections)
    problems = []
    for tok in AMOUNT_RE.findall(text):
        if tok.lstrip("$") not in corpus:
            problems.append(f"amount {tok} is not in the evidence")
    for tok in DATE_RE.findall(text):
        if tok not in corpus:
            problems.append(f"date {tok} is not in the evidence")
    for tok in ID_RE.findall(text):
        if tok not in evidence_ids | policy_ids and tok not in corpus and not DATE_RE.fullmatch(tok):
            problems.append(f"identifier {tok} is not in the evidence")
    # ponytail: plain integers (counts, quantities) are not checked; extend AMOUNT_RE if drafts invent them.
    for s in draft.sections:
        if not s.refs:
            problems.append(f"section '{s.heading}' cites no evidence")
        for ref in s.refs:
            if ref not in evidence_ids | policy_ids:
                problems.append(f"section '{s.heading}' cites unknown ref {ref}")
    expected = DECISION_ACTION.get(economics.get("decision"))
    if expected and draft.action != expected:
        problems.append(f"action {draft.action} does not match the decision {economics.get('decision')} ({expected})")
    if draft.action == "MAKE_OFFER":
        want = economics.get("offer") or {}
        if not draft.offer or (draft.offer.offer_type, draft.offer.amount) != (want.get("offer_type"), want.get("amount")):
            problems.append(f"offer must be {want.get('offer_type')} {want.get('amount')} as computed by economics")
    return problems


# --- payload, approval, submit -------------------------------------------------------------------

def payload_for(draft: Draft) -> dict:
    if draft.action == "MAKE_OFFER":
        return {"note": draft.note, "offer_type": draft.offer.offer_type,
                "offer_amount": {"currency_code": "USD", "value": draft.offer.amount}}
    if draft.action == "ACCEPT_CLAIM":
        return {"input": {"note": draft.note}}
    return {"input": {"evidences": [{"evidence_type": draft.evidence_type or "OTHER", "notes": draft.note,
                                     "documents": [{"name": "evidence.pdf"}]}]}}


def payload_hash(payload: dict, pdf_sha256: str | None) -> str:
    return sha256_hex(r.canonical(payload) + (pdf_sha256 or "").encode())


class _MissingLink(Exception):
    pass


async def submit_approved(pool, pp, dispute_id: str, action: str, payload_hash_: str) -> dict:
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM approvals WHERE dispute_id = %s AND action = %s AND payload_hash = %s "
                "AND consumed_at IS NULL ORDER BY id DESC LIMIT 1 FOR UPDATE", (dispute_id, action, payload_hash_))
            approval = await cur.fetchone()
            if not approval:
                raise ApprovalError("no unconsumed approval for this action and payload")
            case = await (await conn.execute("SELECT * FROM cases WHERE dispute_id = %s FOR UPDATE", (dispute_id,))).fetchone()
            if case["payload_hash"] != payload_hash_ or payload_hash(case["payload"], case["pdf_sha256"]) != payload_hash_:
                raise ApprovalError("payload hash does not match the approved hash")
            if case["dry_run"]:
                response = {"dry_run": True}
            else:
                d = await pp.get_dispute(dispute_id)
                link = next((ln for ln in d.get("links", []) if ln.get("rel") == ACTION_REL[action]), None)
                if not link:
                    raise _MissingLink(ACTION_REL[action])
                body = case["payload"]
                request_id = f"submit-{approval['id']}"
                if action == "MAKE_OFFER":
                    response = await pp.post_link(link["href"], json=body, request_id=request_id)
                else:
                    files = {"input": (None, json.dumps(body["input"]), "application/json")}
                    if action == "PROVIDE_EVIDENCE":
                        files["evidence-file"] = ("evidence.pdf", bytes(case["pdf"]), "application/pdf")
                    response = await pp.post_link(link["href"], files=files, request_id=request_id)
            state = "ACCEPTED" if action == "ACCEPT_CLAIM" else "SUBMITTED"
            await conn.execute("UPDATE approvals SET consumed_at = now(), response = %s, http_status = 200 WHERE id = %s",
                               (Jsonb(response), approval["id"]))
            await conn.execute("UPDATE cases SET state = %s, pending_action = NULL, error = NULL, updated_at = now() "
                               "WHERE dispute_id = %s", (state, dispute_id))
            return {"state": state, "response": response}
    except _MissingLink as e:
        await dbmod.execute(pool, "UPDATE cases SET state = 'NEEDS_ATTENTION', error = %s WHERE dispute_id = %s",
                            f"PayPal no longer offers the {e} action on this dispute", dispute_id)
        raise ApprovalError(f"action {e} not allowed on this dispute now") from e
    except PayPalError as e:
        await dbmod.execute(pool, "UPDATE cases SET state = 'NEEDS_ATTENTION', error = %s WHERE dispute_id = %s",
                            f"PayPal rejected the submission: {e.status} {e.name}", dispute_id)
        raise
