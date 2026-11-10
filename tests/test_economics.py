from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.economics import CaseInputs, decide, normalize_reason
from app.receipts import iso_z

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def ci(**overrides) -> CaseInputs:
    base = dict(
        reason="INR", amount=Decimal("84.00"), created_at=NOW - timedelta(days=1), due_date=None, now=NOW,
        tracking_delivered=False, has_shipping_address=False, capture_sp_status=None, receipt_strength="NONE",
        receipt_violation=False, constraints_match_cart=False, refund_covers_amount=False, distinct_duplicate=False,
        disputes_90d=1, txns_90d=120,
    )
    base.update(overrides)
    return CaseInputs(**base)


def test_normalize_reason():
    assert normalize_reason("MERCHANDISE_OR_SERVICE_NOT_RECEIVED") == "INR"
    assert normalize_reason("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED") == "SNAD"
    assert normalize_reason("UNAUTHORISED") == "UNAUTHORIZED"
    assert normalize_reason("DUPLICATE_TRANSACTION") == "DUPLICATE"
    assert normalize_reason("CREDIT_NOT_PROCESSED") == "CREDIT_NOT_PROCESSED"
    assert normalize_reason("PAYMENT_BY_OTHER_MEANS") == "OTHER"


def test_g1_inr_delivered_seller_protection_fight():
    d = decide(ci(amount=Decimal("80.00"), tracking_delivered=True, has_shipping_address=True))
    assert d["decision"] == "FIGHT" and d["fee"] == "0" and d["sp_eligible"] is True
    assert d["evidence_type"] == "PROOF_OF_FULFILLMENT"


def test_g2_inr_no_tracking_offer():
    assert decide(ci(amount=Decimal("45.00")))["decision"] == "OFFER"


def test_g3_snad_strong_receipt_constraints_fight():
    d = decide(ci(reason="SNAD", receipt_strength="STRONG", constraints_match_cart=True, tracking_delivered=True))
    assert d["decision"] == "FIGHT"


def test_g4_snad_damaged_offer_partial_refund():
    d = decide(ci(reason="SNAD", amount=Decimal("60.00"), tracking_delivered=True))
    assert d["decision"] == "OFFER"
    assert d["offer"] == {"offer_type": "REFUND", "amount": "24.00"}


def test_g5_unauthorized_strong_fight():
    assert decide(ci(reason="UNAUTHORIZED", receipt_strength="STRONG"))["decision"] == "FIGHT"


def test_g6_unauthorized_medium_offer():
    assert decide(ci(reason="UNAUTHORIZED", receipt_strength="MEDIUM"))["decision"] == "OFFER"


def test_g7_receipt_violation_accept():
    d = decide(ci(reason="SNAD", receipt_strength="STRONG", receipt_violation=True))
    assert d["decision"] == "ACCEPT" and "receipt_violation" in d["rules_fired"]


def test_g8_small_order_accept():
    d = decide(ci(amount=Decimal("12.00")))
    assert d["decision"] == "ACCEPT" and d["fee"] == "0"


def test_g9_credit_not_processed_refund_fight():
    d = decide(ci(reason="CREDIT_NOT_PROCESSED", amount=Decimal("50.00"), refund_covers_amount=True))
    assert d["decision"] == "FIGHT" and d["evidence_type"] == "PROOF_OF_REFUND"


def test_g10_duplicate_distinct_fight():
    assert decide(ci(reason="DUPLICATE", distinct_duplicate=True))["decision"] == "FIGHT"


def test_g11_deadline_passed_expired():
    d = decide(ci(due_date=NOW - timedelta(hours=1)))
    assert d["decision"] == "EXPIRED" and d["hours_left"] < 0


def test_g12_high_volume():
    created = NOW - timedelta(days=1)
    d = decide(ci(created_at=created, disputes_90d=2, txns_90d=120))
    assert d["high_volume"] is True and d["hv_warning"] is True
    assert d["fee"] == "30"
    assert d["due_at"] == iso_z(created + timedelta(days=3))


def test_hv_needs_over_100_txns():
    assert decide(ci(disputes_90d=2, txns_90d=100))["high_volume"] is False


def test_warning_at_1pct():
    d = decide(ci(disputes_90d=2, txns_90d=150))
    assert d["hv_warning"] is True and d["high_volume"] is False


def test_p_clamped():
    d = decide(ci(reason="CREDIT_NOT_PROCESSED", refund_covers_amount=True, receipt_strength="STRONG"))
    assert d["p_win"] == 0.95


def test_due_date_from_paypal_wins():
    due = NOW + timedelta(days=4)
    d = decide(ci(due_date=due))
    assert d["due_at"] == iso_z(due) and d["hours_left"] == 96.0
