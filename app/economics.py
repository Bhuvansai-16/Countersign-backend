"""Deterministic dispute economics: fees, deadlines, Seller Protection, expected value, decision.

The priors are configurable assumptions, not measured PayPal win rates (PayPal publishes none).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.receipts import iso_z

REASONS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "INR",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "SNAD",
    "UNAUTHORISED": "UNAUTHORIZED",
    "UNAUTHORIZED": "UNAUTHORIZED",
    "DUPLICATE_TRANSACTION": "DUPLICATE",
    "CREDIT_NOT_PROCESSED": "CREDIT_NOT_PROCESSED",
}
CENT = Decimal("0.01")


def normalize_reason(paypal_reason: str) -> str:
    return REASONS.get((paypal_reason or "").upper(), "OTHER")


@dataclass
class CaseInputs:
    reason: str
    amount: Decimal
    created_at: datetime
    due_date: datetime | None
    now: datetime
    tracking_delivered: bool
    has_shipping_address: bool
    capture_sp_status: str | None
    receipt_strength: str
    receipt_violation: bool
    constraints_match_cart: bool
    refund_covers_amount: bool
    distinct_duplicate: bool
    disputes_90d: int
    txns_90d: int


@dataclass
class Priors:
    base: dict = field(default_factory=lambda: {"INR": .35, "SNAD": .25, "UNAUTHORIZED": .30, "DUPLICATE": .20,
                                                "CREDIT_NOT_PROCESSED": .20, "OTHER": .20})
    bonus_sp_inr: float = .45
    bonus_strong: float = .20
    bonus_delivered: float = .10
    bonus_constraints_snad: float = .25
    bonus_strong_unauth: float = .30
    bonus_refund: float = .75
    bonus_duplicate: float = .60
    labor: Decimal = Decimal("5")
    offer_ratio: Decimal = Decimal("0.4")
    p_offer_accept: Decimal = Decimal("0.5")


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def _money(x: Decimal) -> str:
    return str(x.quantize(CENT, ROUND_HALF_UP))


def decide(ci: CaseInputs, priors: Priors = Priors()) -> dict:
    rules: list[str] = []
    rate = ci.disputes_90d / ci.txns_90d if ci.txns_90d else 0.0
    high_volume = rate >= 0.015 and ci.txns_90d > 100
    hv_warning = rate >= 0.010
    due_at = ci.due_date or ci.created_at + timedelta(days=3 if high_volume else 10)
    hours_left = round((due_at - ci.now).total_seconds() / 3600, 1)

    sp_eligible = (ci.reason == "INR" and ci.tracking_delivered and ci.has_shipping_address
                   and ci.capture_sp_status in (None, "ELIGIBLE", "PARTIALLY_ELIGIBLE"))
    amount = ci.amount
    fee = Decimal(0) if amount < 30 or sp_eligible else Decimal(30 if high_volume else 15)

    p = _d(priors.base.get(ci.reason, priors.base["OTHER"]))
    bonuses = [
        (sp_eligible, priors.bonus_sp_inr, "seller_protection_inr"),
        (ci.receipt_strength == "STRONG", priors.bonus_strong, "strong_receipt"),
        (ci.tracking_delivered and not sp_eligible, priors.bonus_delivered, "tracking_delivered"),
        (ci.reason == "SNAD" and ci.constraints_match_cart and ci.receipt_strength == "STRONG",
         priors.bonus_constraints_snad, "receipt_constraints_match_cart"),
        (ci.reason == "UNAUTHORIZED" and ci.receipt_strength == "STRONG", priors.bonus_strong_unauth, "strong_receipt_unauthorized"),
        (ci.reason == "CREDIT_NOT_PROCESSED" and ci.refund_covers_amount, priors.bonus_refund, "refund_already_issued"),
        (ci.reason == "DUPLICATE" and ci.distinct_duplicate, priors.bonus_duplicate, "distinct_duplicate_orders"),
    ]
    for applies, bonus, name in bonuses:
        if applies:
            p += _d(bonus)
            rules.append(name)
    p = min(max(p, Decimal("0.05")), Decimal("0.95"))

    fight = (1 - p) * (amount + fee) + priors.labor
    accept = amount + fee
    offer_amount = (priors.offer_ratio * amount).quantize(CENT, ROUND_HALF_UP)
    offer = priors.p_offer_accept * offer_amount + (1 - priors.p_offer_accept) * fight + priors.labor

    if hours_left < 0:
        decision = "EXPIRED"
        rules.append("deadline_passed")
    elif ci.receipt_violation:
        decision = "ACCEPT"
        rules.append("receipt_violation")
    else:  # argmin; ties prefer FIGHT, then OFFER (min keeps the first of equal values)
        decision = min((fight, "FIGHT"), (offer, "OFFER"), (accept, "ACCEPT"), key=lambda t: t[0])[1]

    evidence_type = {"CREDIT_NOT_PROCESSED": "PROOF_OF_REFUND", "INR": "PROOF_OF_FULFILLMENT"}.get(ci.reason, "OTHER")
    pr = asdict(priors)
    return {
        "decision": decision,
        "p_win": float(p),
        "fee": str(fee),
        "sp_eligible": sp_eligible,
        "high_volume": high_volume,
        "hv_warning": hv_warning,
        "dispute_rate_90d": round(rate, 4),
        "due_at": iso_z(due_at),
        "hours_left": hours_left,
        "ev": {"fight": _money(fight), "offer": _money(offer), "accept": _money(accept)},
        "offer": {"offer_type": "REFUND_WITH_RETURN" if ci.reason == "SNAD" else "REFUND", "amount": _money(offer_amount)},
        "evidence_type": evidence_type,
        "assumptions": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in pr.items()},
        "rules_fired": rules,
    }
