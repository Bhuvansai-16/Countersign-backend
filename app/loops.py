"""Background loops in the API process: dispute poller (webhook backup) and expiry sweeper."""

import asyncio
import logging
from datetime import datetime, timezone

from app import db as dbmod
from app.webhooks import handle_dispute_event

log = logging.getLogger("countersign.loops")
CLOSED = ["SUBMITTED", "WON", "LOST", "ACCEPTED", "EXPIRED", "MANUAL"]


async def poll_once(state) -> None:
    data = await state.pp.list_disputes({"page_size": 50})
    for item in data.get("items", []):
        known = await dbmod.fetchrow(state.pool, "SELECT raw->>'update_time' AS ut FROM disputes WHERE dispute_id = %s",
                                     item["dispute_id"])
        if known and known["ut"] == item.get("update_time"):
            continue
        etype = "CUSTOMER.DISPUTE.RESOLVED" if item.get("status") == "RESOLVED" else "CUSTOMER.DISPUTE.UPDATED"
        await handle_dispute_event(state, {"event_type": etype, "resource": item})


async def sweep_once(state, now: datetime) -> None:
    await dbmod.execute(state.pool, "UPDATE cases c SET state = 'EXPIRED', updated_at = now() FROM disputes d "
                        "WHERE c.dispute_id = d.dispute_id AND c.state <> ALL(%s) AND d.due_at < %s", CLOSED, now)


async def run_forever(state, poll_every: int = 300, sweep_every: int = 600) -> None:
    await asyncio.sleep(10)  # let startup finish first
    tick = 0
    while True:
        for every, job in ((poll_every, lambda: poll_once(state)),
                           (sweep_every, lambda: sweep_once(state, datetime.now(timezone.utc)))):
            if tick % every == 0:
                try:
                    await job()
                except Exception:
                    log.exception("background job failed")
        await asyncio.sleep(60)
        tick += 60
