"""report_issue: the buyer's agent and the seller settle a problem before it becomes a PayPal dispute.

Flow: agent reports -> deterministic proposal (seller sees it) -> seller approves the offer -> buyer agent
ACCEPTs (refund runs), DECLINEs, or COUNTERs once (back to the seller). Money moves only after both sides agree.
"""

import hashlib
import secrets
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app import db as dbmod
from app import receipts as r
from app.llm import chat_model, json_call
from app.pii import Redactor

ISSUE_TYPES = {"WRONG_ITEM", "DAMAGED", "NOT_RECEIVED", "NOT_AS_DESCRIBED", "OTHER"}
RESOLUTIONS = {"REFUND", "PARTIAL_REFUND", "REPLACEMENT", "RETURN"}
PARTIAL_RATIO = Decimal("0.4")


class IssueError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


class _Summary(BaseModel):
    summary: str


def proposal_hash(proposal: dict) -> str:
    return hashlib.sha256(r.canonical(proposal)).hexdigest()


def _money(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01"), ROUND_HALF_UP))


async def _summary(state, description: str, model) -> str:
    fallback = description[:200]
    if model is None and not state.settings.llm_model:
        return fallback
    try:
        out = await json_call(model or chat_model(state.settings, fast=True),
                              "Summarise the buyer's problem in one short sentence for the seller. JSON: {\"summary\": str}",
                              Redactor().redact(description), _Summary)
        return out.summary[:300]
    except Exception:
        return fallback


async def _issue_for_agent(conn, issue_id: str, agent_id: str, lock: bool = False) -> dict:
    cur = await conn.execute("SELECT i.*, rc.agent_id FROM issues i JOIN receipts rc ON rc.id = i.receipt_id "
                             "WHERE i.id = %s" + (" FOR UPDATE OF i" if lock else ""), (issue_id,))
    row = await cur.fetchone()
    if not row or row["agent_id"] != agent_id:
        raise IssueError(404, "issue not found")
    return row


async def report_issue(state, mandate_id: str, agent_id: str, issue_type: str, description: str,
                       desired_resolution: str, model=None) -> dict:
    if issue_type not in ISSUE_TYPES or desired_resolution not in RESOLUTIONS:
        raise IssueError(422, f"issue_type must be one of {sorted(ISSUE_TYPES)}, desired_resolution one of {sorted(RESOLUTIONS)}")
    rec = await r.load_receipt(state.pool, mandate_id)
    if not rec or rec["agent_id"] != agent_id:
        raise IssueError(404, "mandate not found")
    order = await dbmod.fetchrow(state.pool, "SELECT capture_id, amount FROM orders WHERE receipt_id = %s", mandate_id)
    if rec["status"] != "CAPTURED" or not order or not order["capture_id"]:
        raise IssueError(409, "issues can be reported only for paid orders")
    amount = Decimal(order["amount"])
    if desired_resolution == "REPLACEMENT":
        proposal = {"type": "REPLACEMENT"}
    elif desired_resolution == "PARTIAL_REFUND" or issue_type == "OTHER":
        proposal = {"type": "REFUND", "amount": _money(PARTIAL_RATIO * amount)}
    else:
        proposal = {"type": "REFUND", "amount": _money(amount)}
    proposal["summary"] = await _summary(state, description, model)
    issue_id = "iss_" + secrets.token_hex(8)
    await dbmod.execute(
        state.pool, "INSERT INTO issues (id, receipt_id, capture_id, type, description, desired_resolution, proposal, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'PROPOSED')",
        issue_id, mandate_id, order["capture_id"], issue_type, description, desired_resolution, Jsonb(proposal))
    await r.append_event(state.pool, mandate_id, "ISSUE_REPORTED",
                         {"issue_id": issue_id, "type": issue_type, "desired_resolution": desired_resolution})
    return {"issue_id": issue_id, "state": "PROPOSED", "proposal": proposal}


async def approve_issue(state, issue_id: str, payload_hash: str) -> dict:
    """Seller approves the current proposal; it becomes an offer to the buyer's agent. No money moves yet."""
    async with state.pool.connection() as conn, conn.transaction():
        row = await (await conn.execute("SELECT * FROM issues WHERE id = %s FOR UPDATE", (issue_id,))).fetchone()
        if not row:
            raise IssueError(404, "issue not found")
        if row["state"] != "PROPOSED":
            raise IssueError(409, f"issue is {row['state']}")
        if payload_hash != proposal_hash(row["proposal"]):
            raise IssueError(409, "approval does not match the current proposal")
        action = "REPLACEMENT" if row["proposal"]["type"] == "REPLACEMENT" else "REFUND"
        await conn.execute("INSERT INTO approvals (issue_id, user_id, action, payload_hash) VALUES (%s, 'seller', %s, %s)",
                           (issue_id, action, payload_hash))
        await conn.execute("UPDATE issues SET state = 'OFFERED' WHERE id = %s", (issue_id,))
    await r.append_event(state.pool, row["receipt_id"], "ISSUE_OFFERED", {"issue_id": issue_id, "proposal": row["proposal"]})
    return {"issue_id": issue_id, "state": "OFFERED", "proposal": row["proposal"]}


async def respond_to_proposal(state, issue_id: str, agent_id: str, decision: str, counter_amount: str | None = None) -> dict:
    if decision not in ("ACCEPT", "DECLINE", "COUNTER"):
        raise IssueError(422, "decision must be ACCEPT, DECLINE or COUNTER")
    async with state.pool.connection() as conn, conn.transaction():
        row = await _issue_for_agent(conn, issue_id, agent_id, lock=True)
        if row["state"] != "OFFERED":
            raise IssueError(409, f"there is no open offer (issue is {row['state']})")
        proposal, event, resolution = row["proposal"], None, None
        if decision == "DECLINE":
            new_state = "REJECTED"
        elif decision == "COUNTER":
            if row["countered"]:
                raise IssueError(409, "you can counter only once")
            paid = Decimal((await (await conn.execute("SELECT amount FROM orders WHERE capture_id = %s",
                                                      (row["capture_id"],))).fetchone())["amount"])
            try:
                amount = Decimal(str(counter_amount))
            except (InvalidOperation, TypeError):
                raise IssueError(422, "counter_amount must be a decimal string")
            if not Decimal(0) < amount <= paid:
                raise IssueError(422, f"counter_amount must be between 0 and {_money(paid)}")
            proposal = {"type": "REFUND", "amount": _money(amount), "summary": row["proposal"].get("summary", "")}
            new_state, event = "PROPOSED", ("ISSUE_COUNTERED", {"issue_id": issue_id, "amount": _money(amount)})
            await conn.execute("UPDATE issues SET countered = true, proposal = %s WHERE id = %s", (Jsonb(proposal), issue_id))
        else:  # ACCEPT: execute exactly the offer the seller approved
            cur = await conn.execute("SELECT id FROM approvals WHERE issue_id = %s AND payload_hash = %s AND consumed_at IS NULL "
                                     "ORDER BY id DESC LIMIT 1 FOR UPDATE", (issue_id, proposal_hash(proposal)))
            approval = await cur.fetchone()
            if not approval:
                raise IssueError(409, "the seller has not approved this offer")
            resolution = {"type": proposal["type"]}
            if proposal["type"] == "REFUND":
                refund = await state.pp.refund_capture(row["capture_id"], proposal["amount"], request_id=f"issue-{issue_id}",
                                                       note="Refund agreed with your shopping agent")
                resolution.update(amount=proposal["amount"], refund_id=refund.get("id"))
            await conn.execute("UPDATE approvals SET consumed_at = now(), response = %s, http_status = 200 WHERE id = %s",
                               (Jsonb(resolution), approval["id"]))
            new_state, event = "RESOLVED", ("ISSUE_RESOLVED", {"issue_id": issue_id, **resolution})
        await conn.execute("UPDATE issues SET state = %s, resolution = COALESCE(%s, resolution) WHERE id = %s",
                           (new_state, Jsonb(resolution) if resolution else None, issue_id))
    if event:
        await r.append_event(state.pool, row["receipt_id"], *event)
    return {"issue_id": issue_id, "state": new_state, "proposal": proposal, "resolution": resolution}


async def get_issue_status(state, issue_id: str, agent_id: str) -> dict:
    async with state.pool.connection() as conn:
        row = await _issue_for_agent(conn, issue_id, agent_id)
    return {"issue_id": issue_id, "state": row["state"], "proposal": row["proposal"], "resolution": row["resolution"],
            "countered": row["countered"]}


async def list_issues(state) -> list[dict]:
    rows = await dbmod.fetch(state.pool, "SELECT * FROM issues ORDER BY created_at DESC")
    return [{"issue_id": i["id"], "receipt_id": i["receipt_id"], "type": i["type"], "description": i["description"],
             "desired_resolution": i["desired_resolution"], "proposal": i["proposal"], "state": i["state"],
             "countered": i["countered"], "resolution": i["resolution"], "created_at": i["created_at"],
             "payload_hash": proposal_hash(i["proposal"]) if i["proposal"] else None} for i in rows]
