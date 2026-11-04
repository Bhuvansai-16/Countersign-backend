"""PayPal webhook receiver: verify, deduplicate, answer fast, process in the background."""

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from psycopg.types.json import Jsonb

from app import agent as ag
from app import db as dbmod
from app import disputes as dsp
from app.paypal import PayPalError

log = logging.getLogger("countersign.webhooks")
router = APIRouter()

# PayPal dispute_outcome.outcome_code -> case state. Explicit on purpose: CANCELED_BY_BUYER is a seller win.
OUTCOME_STATE = {
    "RESOLVED_SELLER_FAVOUR": "WON", "CANCELED_BY_BUYER": "WON", "DENIED": "WON",
    "RESOLVED_BUYER_FAVOUR": "LOST", "ACCEPTED": "LOST", "RESOLVED_WITH_PAYOUT": "LOST",
}


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, background: BackgroundTasks):
    s = request.app.state.cs
    try:
        event = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "body is not JSON")
    try:
        verified = await s.pp.verify_webhook(request.headers, event, s.settings.paypal_webhook_id)
    except PayPalError:
        verified = False
    if not verified and not s.settings.allow_simulated_webhooks:
        raise HTTPException(400, "webhook signature verification failed")
    resource = event.get("resource") or {}
    row = await dbmod.fetchrow(
        s.pool, "INSERT INTO webhook_events (id, event_type, verified, resource_id, payload) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        event.get("id"), event.get("event_type", ""), verified, resource.get("dispute_id") or resource.get("id"), Jsonb(event))
    if not row:
        return {"ok": True, "duplicate": True}
    if event.get("event_type", "").startswith("CUSTOMER.DISPUTE."):
        background.add_task(handle_dispute_event, s, event)
    return {"ok": True}


async def handle_dispute_event(state, event: dict) -> None:
    dispute_id = (event.get("resource") or {}).get("dispute_id")
    if not dispute_id:
        return
    try:
        await dsp.triage(state.pool, state.pp, dispute_id, state.settings)
        if event.get("event_type", "").endswith("RESOLVED"):
            raw = (await dbmod.fetchrow(state.pool, "SELECT raw FROM disputes WHERE dispute_id = %s", dispute_id))["raw"]
            code = (raw.get("dispute_outcome") or {}).get("outcome_code") or ""
            await dbmod.execute(state.pool, "UPDATE cases SET state = COALESCE(%s, state), outcome = %s, updated_at = now() "
                                "WHERE dispute_id = %s", OUTCOME_STATE.get(code), code or None, dispute_id)
        else:
            await ag.run_case(state, dispute_id)  # guarded: at most one run per case
    except Exception:  # never let one bad event kill the worker; the poller retries later
        log.exception("dispute event %s failed", event.get("id"))
    if event.get("id"):
        await dbmod.execute(state.pool, "UPDATE webhook_events SET processed_at = now() WHERE id = %s", event["id"])
