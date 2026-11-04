"""Seller console routes: case queue, case detail, draft edits, approve / override / reject, PDF, metrics."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app import agent as ag
from app import auth
from app import db as dbmod
from app import disputes as dsp
from app.api import cs
from app.pii import Redactor

router = APIRouter(dependencies=[Depends(auth.require_seller)])
CLOSED = ("SUBMITTED", "WON", "LOST", "ACCEPTED", "EXPIRED", "MANUAL")
EV_KEY = {"FIGHT": "fight", "OFFER": "offer", "ACCEPT": "accept"}


class AnalyzeIn(BaseModel):
    force: bool = False


class ApproveIn(BaseModel):
    action: Literal["PROVIDE_EVIDENCE", "MAKE_OFFER", "ACCEPT_CLAIM"]
    payload_hash: str


class OverrideIn(BaseModel):
    decision: Literal["FIGHT", "OFFER", "ACCEPT"]
    reason: str = ""


async def _case(s, dispute_id: str) -> dict:
    row = await dbmod.fetchrow(s.pool, "SELECT * FROM cases WHERE dispute_id = %s", dispute_id)
    if not row:
        raise HTTPException(404, "case not found")
    return row


def _hours_left(due: datetime | None) -> float | None:
    return round((due - datetime.now(timezone.utc)).total_seconds() / 3600, 1) if due else None


@router.get("/api/cases")
async def cases(state: str | None = None, reason: str | None = None, due_before: datetime | None = None, s=Depends(cs)):
    rows = await dbmod.fetch(s.pool, "SELECT c.*, d.stage, d.status, d.amount, d.currency, d.due_at FROM cases c "
                             "JOIN disputes d USING (dispute_id) ORDER BY d.due_at NULLS LAST, c.dispute_id")
    out = []
    for row in rows:
        tri, econ = row["triage"] or {}, ag.effective_economics(row)
        if (state and row["state"] != state) or (reason and tri.get("reason") != reason):
            continue
        if due_before and row["due_at"] and row["due_at"] > due_before:
            continue
        ev, key = econ.get("ev") or {}, EV_KEY.get(econ.get("decision"))
        saving = str(Decimal(ev["accept"]) - Decimal(ev[key])) if key and ev.get(key) and ev.get("accept") else None
        required = len(dsp.REQUIRED_EVIDENCE.get(tri.get("reason"), []))
        completeness = None if row["evidence"] is None else (
            1.0 if not required else round(1 - len(row["evidence_gaps"] or []) / required, 2))
        out.append({
            "dispute_id": row["dispute_id"], "reason": tri.get("reason"), "stage": row["stage"], "status": row["status"],
            "amount": str(row["amount"]) if row["amount"] is not None else None, "currency": row["currency"],
            "due_at": row["due_at"], "hours_left": _hours_left(row["due_at"]), "fee": econ.get("fee"),
            "sp_eligible": econ.get("sp_eligible"), "receipt_strength": row["receipt_strength"],
            "evidence_completeness": completeness, "decision": econ.get("decision"), "ev_saving": saving,
            "state": row["state"], "outcome": row["outcome"], "dry_run": row["dry_run"],
        })
    return out


@router.get("/api/cases/{dispute_id}")
async def case_detail(dispute_id: str, s=Depends(cs)):
    row = await _case(s, dispute_id)
    raw = (await dbmod.fetchrow(s.pool, "SELECT raw FROM disputes WHERE dispute_id = %s", dispute_id))["raw"]
    evidence = row["evidence"] or []
    hidden = {"pdf", "payload", "evidence", "evidence_gaps", "draft", "check_issues", "economics", "pending_action", "payload_hash"}
    return {
        "dispute": Redactor().redact(raw),
        "case": {k: v for k, v in row.items() if k not in hidden},
        "evidence": evidence, "evidence_gaps": row["evidence_gaps"] or [],
        "draft": row["draft"], "check_issues": row["check_issues"] or [],
        "economics": ag.effective_economics(row),
        "receipt_verification": next((i["data"] for i in evidence if i["kind"] == "RECEIPT_VERIFICATION"), None),
        "pending_action": row["pending_action"], "payload_hash": row["payload_hash"],
        "pdf_url": f"/api/cases/{dispute_id}/pdf" if row["pdf"] else None,
        "allowed_actions": [ln["rel"] for ln in raw.get("links", []) if ln.get("rel") in dsp.ACTION_REL.values()],
    }


@router.post("/api/cases/{dispute_id}/analyze")
async def analyze(dispute_id: str, background: BackgroundTasks, body: AnalyzeIn | None = None, s=Depends(cs)):
    row = await _case(s, dispute_id)
    background.add_task(ag.run_case, s, dispute_id, bool(body and body.force))
    return {"state": row["state"], "scheduled": True}


@router.put("/api/cases/{dispute_id}/draft")
async def edit_draft(dispute_id: str, draft: dsp.Draft, s=Depends(cs)):
    row = await _case(s, dispute_id)
    if row["state"] != "AWAITING_APPROVAL":
        raise HTTPException(409, f"case is {row['state']}, drafts can be edited only while awaiting approval")
    evidence = row["evidence"] or []
    pb = dsp.load_playbook(row["triage"]["reason"], any(i["kind"] == "RECEIPT" for i in evidence))
    problems = dsp.check_draft(draft, evidence, pb["policy_ids"], ag.effective_economics(row))
    if problems:
        return JSONResponse({"problems": problems}, status_code=422)
    return await ag.persist_draft(s, dispute_id, draft, evidence, pb, row)


@router.post("/api/cases/{dispute_id}/approve")
async def approve(dispute_id: str, body: ApproveIn, s=Depends(cs)):
    row = await _case(s, dispute_id)
    if row["state"] != "AWAITING_APPROVAL":
        raise HTTPException(409, f"case is {row['state']}")
    if row["check_issues"]:
        raise HTTPException(409, "the draft has unresolved problems")
    if body.payload_hash != row["payload_hash"] or (row["draft"] or {}).get("action") != body.action:
        raise HTTPException(409, "this approval does not match the current draft; reload and approve again")
    await dbmod.execute(s.pool, "INSERT INTO approvals (dispute_id, user_id, action, payload_hash) VALUES (%s, 'seller', %s, %s)",
                        dispute_id, body.action, body.payload_hash)
    args = {"action": body.action, "payload_hash": body.payload_hash}
    if ((row["pending_action"] or {}).get("args") or {}) == args:
        decision = {"type": "approve"}
    else:
        decision = {"type": "edit", "edited_action": {"name": ag.SUBMIT_TOOL, "args": args}}
    return {"state": await ag.resume_case(s, dispute_id, decision)}


@router.post("/api/cases/{dispute_id}/override")
async def override(dispute_id: str, body: OverrideIn, s=Depends(cs)):
    row = await _case(s, dispute_id)
    await dbmod.execute(s.pool, "UPDATE cases SET override = %s::jsonb WHERE dispute_id = %s",
                        body.model_dump_json(), dispute_id)
    state = row["state"]
    if state == "AWAITING_APPROVAL":
        message = f"Seller changed the decision to {body.decision}: {body.reason}. Redo the draft for {body.decision}."
        state = await ag.resume_case(s, dispute_id, {"type": "reject", "message": message})
    return {"state": state, "override": body.model_dump()}


@router.post("/api/cases/{dispute_id}/reject")
async def reject(dispute_id: str, s=Depends(cs)):
    await _case(s, dispute_id)
    row = await dbmod.fetchrow(s.pool, "UPDATE cases SET state = 'MANUAL', pending_action = NULL, updated_at = now() "
                               "WHERE dispute_id = %s AND state IN ('NEW', 'AWAITING_APPROVAL', 'NEEDS_ATTENTION') "
                               "RETURNING state", dispute_id)
    if not row:
        raise HTTPException(409, "case can no longer be rejected")
    return {"state": row["state"]}


@router.get("/api/cases/{dispute_id}/pdf")
async def pdf(dispute_id: str, s=Depends(cs)):
    row = await _case(s, dispute_id)
    if not row["pdf"]:
        raise HTTPException(404, "no evidence PDF for this case")
    return Response(bytes(row["pdf"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{dispute_id}-evidence.pdf"'})


@router.get("/api/metrics")
async def metrics(s=Depends(cs)):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=90)
    m = await dbmod.fetchrow(
        s.pool,
        "SELECT (SELECT count(*) FROM orders WHERE capture_id IS NOT NULL AND created_at >= %s) "
        "     + (SELECT count(*) FROM history_txns WHERE at >= %s) AS txns, "
        "       (SELECT count(*) FROM disputes WHERE created_at_pp >= %s) "
        "     + (SELECT count(*) FROM history_txns WHERE at >= %s AND disputed) AS disputes, "
        "       (SELECT count(*) FROM cases WHERE state <> ALL(%s)) AS open_cases, "
        "       (SELECT count(*) FROM cases c JOIN disputes d USING (dispute_id) WHERE c.state <> ALL(%s) "
        "          AND d.due_at < %s) AS due_48h, "
        "       (SELECT count(*) FROM cases WHERE state = 'WON') AS won, "
        "       (SELECT count(*) FROM cases WHERE state = 'LOST') AS lost, "
        "       (SELECT COALESCE(sum(d.amount), 0) FROM cases c JOIN disputes d USING (dispute_id) WHERE c.state = 'WON') AS defended, "
        "       EXISTS (SELECT 1 FROM history_txns WHERE simulated) AS simulated",
        since, since, since, since, list(CLOSED), list(CLOSED), now + timedelta(hours=48))
    rate = m["disputes"] / m["txns"] if m["txns"] else 0.0
    return {"dispute_rate_90d": round(rate, 4), "txns_90d": m["txns"], "disputes_90d": m["disputes"],
            "hv_threshold": 0.015, "warn_threshold": 0.010, "open_cases": m["open_cases"], "due_48h": m["due_48h"],
            "won": m["won"], "lost": m["lost"], "dollars_defended": str(m["defended"]), "simulated_history": m["simulated"]}
