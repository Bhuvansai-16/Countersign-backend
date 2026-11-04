from app.disputes import Draft
from app.pdf import build_evidence_pdf

CASE = {"dispute_id": "PP-D-1001", "merchant_name": "Maya's Loom (sandbox)",
        "triage": {"reason": "INR", "amount": "84.00", "currency": "USD"}}
EVIDENCE = [{"id": "E1", "kind": "TRACKING", "summary": "USPS 9400100000000000000000 DELIVERED", "data": {}}]
DRAFT = Draft(action="PROVIDE_EVIDENCE", evidence_type="PROOF_OF_FULFILLMENT", note="Delivered.",
              sections=[{"heading": "Delivery", "text": "Delivered by USPS.", "refs": ["E1", "POL-INR-2"]}])
RECEIPT = {"receipt": {"id": "rcpt_1", "seal_hash": "ab" * 32}, "paypal_custom_id": "cs1:" + "ab" * 32,
           "events": [{"seq": 0, "type": "MANDATE_CREATED", "at": "2026-11-04T10:00:00Z", "hash": "cd" * 32}],
           "verification": {"strength": "STRONG", "sig_ok": True, "chain_ok": True, "custom_id_match": True}}
CITED = [{"id": "POL-INR-2", "text": "PayPal accepts tracking as evidence.", "source": "https://www.paypal.com/x"}]
URL = "https://countersign.example/verify/rcpt_1"


def _pdf(**kw):
    args = dict(case=CASE, draft=DRAFT, evidence=EVIDENCE, receipt_view=RECEIPT, verify_url=URL, cited_policy=CITED, compress=False)
    args.update(kw)
    return build_evidence_pdf(**args)


def test_pdf_header():
    assert _pdf().startswith(b"%PDF")


def test_contains_dispute_and_verify_url():
    pdf = _pdf()
    assert b"PP-D-1001" in pdf
    assert URL.encode() in pdf
    assert b"POL-INR-2" in pdf and b"STRONG" in pdf


def test_excludes_uncited_playbook_text():
    assert b"Goal: prove the item was shipped" not in _pdf()


def test_pdf_without_receipt():
    assert _pdf(receipt_view=None).startswith(b"%PDF")
