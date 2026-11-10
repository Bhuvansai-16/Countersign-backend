import json
from datetime import datetime, timezone

import httpx
import pytest

from app import db as dbmod
from app import disputes as dsp
from app import mandates as m
from tests.conftest import FakePayPal
from tests.test_mandates import CAPTURED, ORDER_CREATED, _seed_products

pytestmark = pytest.mark.anyio
BASE = "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1001"


def dispute_json(**over):
    d = {
        "dispute_id": "PP-D-1001",
        "create_time": "2026-11-07T10:00:00.000Z",
        "update_time": "2026-11-07T10:05:00.000Z",
        "disputed_transactions": [{"seller_transaction_id": "C1", "buyer_transaction_id": "B1",
                                   "buyer": {"name": "Sam Rivera"}}],
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_amount": {"currency_code": "USD", "value": "84.00"},
        "seller_response_due_date": "2026-11-17T10:00:00.000Z",
        "dispute_life_cycle_stage": "INQUIRY",
        "messages": [{"posted_by": "BUYER", "time_posted": "2026-11-07T10:00:00.000Z", "content": "It never arrived."}],
        "links": [{"rel": "self", "href": BASE, "method": "GET"},
                  {"rel": "provide_evidence", "href": BASE + "/provide-evidence", "method": "POST"},
                  {"rel": "make_offer", "href": BASE + "/make-offer", "method": "POST"},
                  {"rel": "accept_claim", "href": BASE + "/accept-claim", "method": "POST"}],
    }
    d.update(over)
    return d


def pp_for(dispute, extra=None):
    sent = {}

    def create(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json=ORDER_CREATED)

    routes = {
        ("GET", "/v1/customer/disputes/PP-D-1001"): dispute,
        ("POST", "/v2/checkout/orders"): create,
        ("POST", "/v2/checkout/orders/O1/capture"): CAPTURED,
        ("GET", "/v2/checkout/orders/O1"): lambda req: httpx.Response(200, json={
            "id": "O1", "purchase_units": [{"custom_id": sent["body"]["purchase_units"][0]["custom_id"]}]}),
        ("POST", "/v1/customer/disputes/PP-D-1001/"): {"links": []},
    }
    routes.update(extra or {})
    return FakePayPal(routes)


async def paid_order(db, settings, pp):
    await _seed_products(db)
    md = await m.create_mandate(db, settings, instruction="Buy one linen throw in sage under $90", max_amount="90.00",
                                currency="USD", attributes={"color": "sage"}, items=[{"sku": "THROW-SAGE", "qty": 1}],
                                agent_id="agt_test", agent_platform="custom")
    await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "hosted_page_click", "UA")
    await m.capture(db, pp.client, "O1")
    return md["mandate_id"]


NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


async def test_ingest_maps_to_receipt(db, settings):
    pp = pp_for(dispute_json())
    rid = await paid_order(db, settings, pp)
    ctx = await dsp.ingest(db, pp.client, "PP-D-1001", settings)
    assert ctx["receipt"]["id"] == rid
    assert ctx["order"]["paypal_order_id"] == "O1"
    row = await dbmod.fetchrow(db, "SELECT capture_id, paypal_order_id, reason FROM disputes")
    assert row == {"capture_id": "C1", "paypal_order_id": "O1", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}


async def test_triage_strong_receipt_and_paypal_due_date(db, settings):
    pp = pp_for(dispute_json())
    await paid_order(db, settings, pp)
    case = await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    assert case["state"] == "NEW"
    assert case["receipt_strength"] == "STRONG"
    assert case["triage"]["due_at"] == "2026-11-17T10:00:00Z"
    assert case["economics"]["decision"] in ("FIGHT", "OFFER", "ACCEPT")


async def test_triage_without_receipt_or_due_date(db, settings):
    d = dispute_json(seller_response_due_date=None)
    d["disputed_transactions"][0]["seller_transaction_id"] = "UNKNOWN-CAPTURE"
    pp = pp_for(d)
    case = await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    assert case["state"] == "NEW"
    assert case["receipt_strength"] == "NONE"
    assert case["triage"]["due_at"] == "2026-11-17T10:00:00Z"  # create time + 10 days


async def test_gaps_inr_without_tracking(db, settings):
    pp = pp_for(dispute_json())
    await paid_order(db, settings, pp)
    await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    items, gaps = await dsp.gather_evidence(db, pp.client, "PP-D-1001", settings)
    kinds = {i["kind"] for i in items}
    assert {"DISPUTE", "ORDER", "CAPTURE", "RECEIPT", "RECEIPT_VERIFICATION", "BUYER_MESSAGES"} <= kinds
    assert "TRACKING" in gaps
    assert [i["id"] for i in items] == [f"E{n}" for n in range(1, len(items) + 1)]


def test_load_playbook_has_policy_ids():
    pb = dsp.load_playbook("INR", has_receipt=True)
    assert "POL-INR-1" in pb["policy_ids"]
    assert any(p.startswith("POL-AGENT-") for p in pb["policy_ids"])
    assert "Source: https://" in pb["text"]


EVIDENCE = [
    {"id": "E1", "kind": "DISPUTE", "summary": "Dispute PP-D-1001 for 84.00 USD", "data": {"dispute_id": "PP-D-1001", "create_time": "2026-11-07T10:00:00.000Z"}},
    {"id": "E2", "kind": "TRACKING", "summary": "USPS 9400100000000000000000 DELIVERED", "data": {"tracking_number": "9400100000000000000000"}},
]
ECON_FIGHT = {"decision": "FIGHT", "offer": {"offer_type": "REFUND", "amount": "33.60"}}


def draft(**over):
    base = {"action": "PROVIDE_EVIDENCE", "evidence_type": "PROOF_OF_FULFILLMENT",
            "note": "Order for $84.00 was delivered; see tracking 9400100000000000000000.",
            "sections": [{"heading": "Delivery", "text": "Dispute PP-D-1001 opened 2026-11-07.", "refs": ["E1", "E2", "POL-INR-1"]}]}
    base.update(over)
    return dsp.Draft(**base)


def test_check_draft_passes_clean_draft():
    assert dsp.check_draft(draft(), EVIDENCE, {"POL-INR-1"}, ECON_FIGHT) == []


def test_check_draft_catches_invented_amount():
    problems = dsp.check_draft(draft(note="Buyer paid $91.00 and it was delivered."), EVIDENCE, {"POL-INR-1"}, ECON_FIGHT)
    assert len(problems) == 1 and "91.00" in problems[0]


def test_check_draft_catches_invented_id_and_date():
    bad = draft(sections=[{"heading": "x", "text": "Tracking 9400999999999999999999 on 2026-12-01.", "refs": ["E2"]}])
    problems = dsp.check_draft(bad, EVIDENCE, set(), ECON_FIGHT)
    assert any("9400999999999999999999" in p for p in problems) and any("2026-12-01" in p for p in problems)


def test_check_draft_catches_unknown_ref_and_missing_ref():
    bad = draft(sections=[{"heading": "a", "text": "ok", "refs": ["E9"]}, {"heading": "b", "text": "ok", "refs": []}])
    problems = dsp.check_draft(bad, EVIDENCE, set(), ECON_FIGHT)
    assert any("E9" in p for p in problems) and any("'b'" in p for p in problems)


def test_check_draft_offer_must_match_economics():
    econ = {"decision": "OFFER", "offer": {"offer_type": "REFUND", "amount": "33.60"}}
    offer = draft(action="MAKE_OFFER", evidence_type=None, note="We offer a partial refund.",
                  offer={"offer_type": "REFUND", "amount": "40.00"})
    assert any("offer" in p for p in dsp.check_draft(offer, EVIDENCE, set(), econ))
    assert any("action" in p for p in dsp.check_draft(draft(), EVIDENCE, {"POL-INR-1"}, econ))


def test_check_draft_allows_amounts_computed_by_economics():
    econ = {"decision": "OFFER", "offer": {"offer_type": "REFUND", "amount": "16.80"}}
    offer = draft(action="MAKE_OFFER", evidence_type=None, note="We offer a $16.80 refund to resolve this.",
                  sections=[{"heading": "Offer", "text": "Partial refund offered.", "refs": ["E1"]}],
                  offer={"offer_type": "REFUND", "amount": "16.80"})
    assert dsp.check_draft(offer, EVIDENCE, set(), econ) == []


async def ready_case(db, settings, pp, action="PROVIDE_EVIDENCE", approve=True, dry_run=False):
    await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    d = draft() if action == "PROVIDE_EVIDENCE" else draft(action=action, evidence_type=None, note="Offer",
                                                           offer={"offer_type": "REFUND", "amount": "33.60"})
    payload = dsp.payload_for(d)
    pdf = b"%PDF-1.4 test"
    pdf_sha = dsp.sha256_hex(pdf)
    h = dsp.payload_hash(payload, pdf_sha)
    await dbmod.execute(db, "UPDATE cases SET draft = %s, payload = %s, pdf = %s, pdf_sha256 = %s, payload_hash = %s, "
                        "dry_run = %s, state = 'AWAITING_APPROVAL' WHERE dispute_id = 'PP-D-1001'",
                        json.dumps(d.model_dump()), json.dumps(payload), pdf, pdf_sha, h, dry_run)
    if approve:
        await dbmod.execute(db, "INSERT INTO approvals (dispute_id, user_id, action, payload_hash) VALUES ('PP-D-1001', 'seller', %s, %s)", action, h)
    return h


async def test_submit_without_approval_raises(db, settings):
    pp = pp_for(dispute_json())
    h = await ready_case(db, settings, pp, approve=False)
    with pytest.raises(dsp.ApprovalError):
        await dsp.submit_approved(db, pp.client, "PP-D-1001", "PROVIDE_EVIDENCE", h)
    assert pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/") == []


async def test_submit_hash_mismatch_raises(db, settings):
    pp = pp_for(dispute_json())
    h = await ready_case(db, settings, pp)
    await dbmod.execute(db, "UPDATE cases SET payload = %s WHERE dispute_id = 'PP-D-1001'", json.dumps({"tampered": True}))
    with pytest.raises(dsp.ApprovalError, match="hash"):
        await dsp.submit_approved(db, pp.client, "PP-D-1001", "PROVIDE_EVIDENCE", h)
    assert pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/") == []


async def test_submit_once_on_double_approve(db, settings):
    pp = pp_for(dispute_json())
    h = await ready_case(db, settings, pp)
    out = await dsp.submit_approved(db, pp.client, "PP-D-1001", "PROVIDE_EVIDENCE", h)
    assert out["state"] == "SUBMITTED"
    with pytest.raises(dsp.ApprovalError):
        await dsp.submit_approved(db, pp.client, "PP-D-1001", "PROVIDE_EVIDENCE", h)
    posts = pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/provide-evidence")
    assert len(posts) == 1
    assert b'name="evidence-file"' in posts[0].content and b'name="input"' in posts[0].content


async def test_submit_offer_posts_json(db, settings):
    pp = pp_for(dispute_json())
    h = await ready_case(db, settings, pp, action="MAKE_OFFER")
    await dsp.submit_approved(db, pp.client, "PP-D-1001", "MAKE_OFFER", h)
    body = json.loads(pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/make-offer")[0].content)
    assert body == {"note": "Offer", "offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "33.60"}}


async def test_submit_missing_link_needs_attention(db, settings):
    d = dispute_json()
    d["links"] = [link for link in d["links"] if link["rel"] != "provide_evidence"]
    pp = pp_for(d)
    h = await ready_case(db, settings, pp)
    with pytest.raises(dsp.ApprovalError, match="not allowed"):
        await dsp.submit_approved(db, pp.client, "PP-D-1001", "PROVIDE_EVIDENCE", h)
    assert (await dbmod.fetchrow(db, "SELECT state FROM cases"))["state"] == "NEEDS_ATTENTION"
    assert (await dbmod.fetchrow(db, "SELECT consumed_at FROM approvals"))["consumed_at"] is None


async def test_dry_run_skips_post(db, settings):
    pp = pp_for(dispute_json())
    h = await ready_case(db, settings, pp, dry_run=True)
    out = await dsp.submit_approved(db, pp.client, "PP-D-1001", "PROVIDE_EVIDENCE", h)
    assert out["response"] == {"dry_run": True}
    assert pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/") == []


async def test_receipt_not_strong_when_paypal_unreachable(db, settings):
    pp = pp_for(dispute_json())
    await paid_order(db, settings, pp)
    pp.routes[("GET", "/v2/checkout/orders/O1")] = httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"})
    case = await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    assert case["receipt_strength"] == "WEAK"
