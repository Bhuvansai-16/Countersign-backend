"""Sandbox-only helpers: create disputes as a buyer, drive PayPal's dispute simulators, run the judge demo."""

import base64
import json
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app import agent as ag
from app import auth
from app import db as dbmod
from app import disputes as dsp
from app.api import cs

router = APIRouter(dependencies=[Depends(auth.require_seller)])
SIM_ACTIONS = {"require-evidence", "adjudicate", "escalate"}


def _is_sandbox(s):
    return s.settings.paypal_base_url.rstrip("/") == "https://api-m.sandbox.paypal.com"


def _sim_enabled(s=Depends(cs)):
    if not _is_sandbox(s) or not s.settings.simulator_enabled:
        raise HTTPException(404)
    return s


@router.get("/api/sim/status")
async def sim_status(s=Depends(cs)):
    sandbox = _is_sandbox(s)
    return {
        "simulator_enabled": sandbox and s.settings.simulator_enabled,
        "prepared_cases_enabled": sandbox and s.settings.demo_enabled and bool(s.settings.demo_dispute_pool),
    }


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()


class CreateDisputeIn(BaseModel):
    buyer_transaction_id: str
    reason: Literal["MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"] = \
        "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    amount: str
    buyer_email: str
    accelerated: bool = True


@router.post("/api/sim/disputes")
async def create_dispute(body: CreateDisputeIn, s=Depends(_sim_enabled)):
    """Buyer-side sandbox dispute. PayPal-Auth-Assertion construction is undocumented; see docs/verified.md (S3)."""
    assertion = _b64url({"alg": "none"}) + "." + _b64url({"iss": s.settings.paypal_client_id, "email": body.buyer_email}) + "."
    payload = {
        "disputed_transactions": [{"buyer_transaction_id": body.buyer_transaction_id}],
        "reason": body.reason,
        "dispute_amount": {"currency_code": "USD", "value": body.amount},
        "extensions": {"merchant_contacted": False},
    }
    if body.accelerated:
        payload["evidences"] = [{"notes": "ACCELERATED_RESPONSE_ENABLED"}]
    out = await s.pp.request("POST", "/v1/customer/disputes", files={"input": (None, json.dumps(payload), "application/json")},
                             headers={"PayPal-Auth-Assertion": assertion})
    self_link = next((ln["href"] for ln in out.get("links", []) if ln.get("rel") == "self"), "")
    return {"dispute_id": self_link.rstrip("/").rsplit("/", 1)[-1] or None}


@router.post("/api/sim/disputes/{dispute_id}/{action}")
async def simulate(dispute_id: str, action: str, body: dict, s=Depends(_sim_enabled)):
    """Runs a PayPal sandbox simulator action via the dispute's own HATEOAS link (never a hand-built URL)."""
    if action not in SIM_ACTIONS:
        raise HTTPException(404, f"unknown simulator action {action}")
    d = await s.pp.get_dispute(dispute_id)
    link = next((ln for ln in d.get("links", []) if ln.get("href", "").endswith("/" + action)), None)
    if not link:
        raise HTTPException(404, f"PayPal does not offer {action} on this dispute now (status {d.get('status')})")
    return await s.pp.post_link(link["href"], json=body, request_id=None)


def _demo_enabled(s=Depends(cs)):
    if not _is_sandbox(s) or not s.settings.demo_enabled:
        raise HTTPException(404)
    return s


@router.post("/api/demo/run")
async def demo_run(background: BackgroundTasks, s=Depends(_demo_enabled)):
    """Judge demo: next unused pre-created dispute from DEMO_DISPUTE_POOL; once used up, re-run the latest
    pool case as a labelled dry run (analysis and approval are real, submission is simulated)."""
    pool_ids = list(s.settings.demo_dispute_pool)
    if not pool_ids:
        raise HTTPException(409, "DEMO_DISPUTE_POOL is empty")
    used = {r["dispute_id"] for r in await dbmod.fetch(s.pool, "SELECT dispute_id FROM cases WHERE dispute_id = ANY(%s)", pool_ids)}
    fresh = next((d for d in pool_ids if d not in used), None)
    if fresh:
        await dsp.triage(s.pool, s.pp, fresh, s.settings)
        background.add_task(ag.run_case, s, fresh)
        return {"dispute_id": fresh, "mode": "pool"}
    latest = await dbmod.fetchrow(s.pool, "SELECT dispute_id FROM cases WHERE dispute_id = ANY(%s) ORDER BY updated_at DESC LIMIT 1", pool_ids)
    await dbmod.execute(s.pool, "UPDATE cases SET dry_run = true, state = 'NEW', outcome = NULL, override = NULL, "
                        "updated_at = now() WHERE dispute_id = %s", latest["dispute_id"])
    background.add_task(ag.run_case, s, latest["dispute_id"])
    return {"dispute_id": latest["dispute_id"], "mode": "dry_run"}
