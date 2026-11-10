"""Dispute analyst built on LangChain Deep Agents.

The LLM plans, picks tools and writes the narrative. Every fact and number comes from deterministic tools.
The only write tool is gated twice: Deep Agents' interrupt_on pauses before it runs, and the tool itself
(disputes.submit_approved) refuses to post without a matching, unconsumed seller approval.
"""

import uuid

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from psycopg.types.json import Jsonb

from app import db as dbmod
from app import disputes as dsp
from app import mandates as m
from app.llm import chat_model
from app.pdf import build_evidence_pdf
from app.pii import Redactor

SUBMIT_TOOL = "submit_dispute_response"
NUDGE = "Continue: write the draft with build_evidence_pack, then call submit_dispute_response with its payload_hash."

SYSTEM_PROMPT = """You are a dispute analyst for a small PayPal seller. You prepare the seller's response to one PayPal dispute.

Rules:
- Use only facts returned by your tools. Never invent amounts, dates, tracking numbers, IDs or policy.
- Buyer personal data appears as tokens like <NAME_1> or <EMAIL_1>. Keep the tokens exactly as they are.
- Follow the decision from compute_economics (FIGHT -> PROVIDE_EVIDENCE, OFFER -> MAKE_OFFER with exactly the
  offer type and amount it gives, ACCEPT -> ACCEPT_CLAIM). Explain it; do not change it.
- Every section of your draft cites evidence IDs (E1, E2, ...) or policy IDs (POL-...) in its refs.
- Write for a PayPal reviewer: concise, factual, chronological. The note is at most 2000 characters.

Work in this order:
1. get_case_facts, get_order_evidence, verify_receipt, compute_economics, load_playbook.
2. Write the draft. Ask the fact-checker subagent to review it, then fix what it reports.
3. build_evidence_pack(draft). If it returns problems, fix the draft and call it again (at most twice).
4. submit_dispute_response(action, payload_hash) with the payload_hash from build_evidence_pack.
   This pauses for the seller's approval; it is the only way anything reaches PayPal.
"""

FACT_CHECKER_PROMPT = """You check a PayPal dispute draft against the evidence. Call get_order_evidence and
check_draft. List every claim in the draft that the evidence does not support (amounts, dates, tracking numbers,
IDs, policy statements without a POL- ref). Reply with the list only, or "no problems"."""


async def _case(state, dispute_id: str) -> dict:
    return await dbmod.fetchrow(state.pool, "SELECT * FROM cases WHERE dispute_id = %s", dispute_id)


def effective_economics(case: dict) -> dict:
    econ = dict(case["economics"] or {})
    if case.get("override"):
        econ["decision"] = case["override"]["decision"]
    return econ


async def persist_draft(state, dispute_id: str, real: dsp.Draft, evidence: list[dict], pb: dict, case: dict) -> dict:
    """Save a checked draft (real values, no PII tokens) with its PDF and exact payload; return the payload hash.
    Used by the agent's build_evidence_pack tool and by the seller's draft edits."""
    payload, pdf, pdf_sha = dsp.payload_for(real), None, None
    if real.action == "PROVIDE_EVIDENCE":
        rec = next((i for i in evidence if i["kind"] == "RECEIPT"), None)
        rid = rec["data"]["receipt_id"] if rec else None
        view = await m.receipt_with_verification(state.pool, state.pp, state.settings, rid) if rid else None
        base = state.settings.public_base_url
        cited = dsp.policy_lines(pb["text"], {ref for s in real.sections for ref in s.refs})
        pdf = build_evidence_pdf({**case, "merchant_name": state.settings.merchant_name}, real, evidence,
                                 view, f"{base}/verify/{rid}" if rid else base, cited)
        pdf_sha = dsp.sha256_hex(pdf)
    h = dsp.payload_hash(payload, pdf_sha)
    await dbmod.execute(
        state.pool, "UPDATE cases SET draft = %s, payload = %s, pdf = %s, pdf_sha256 = %s, payload_hash = %s, "
        "check_issues = '[]', updated_at = now() WHERE dispute_id = %s",
        Jsonb(real.model_dump()), Jsonb(payload), pdf, pdf_sha, h, dispute_id)
    return {"payload_hash": h, "action": real.action}


def make_tools(state, dispute_id: str, redactor: Redactor) -> list[StructuredTool]:
    """Tools bound to one dispute: the agent cannot reach any other case."""
    cache: dict = {}

    async def _evidence() -> dict:
        if "evidence" not in cache:
            items, gaps = await dsp.gather_evidence(state.pool, state.pp, dispute_id, state.settings)
            cache.update(evidence=items, gaps=gaps, red=redactor.redact(items))
        return cache

    async def _playbook() -> dict:
        if "pb" not in cache:
            ev = await _evidence()
            case = await _case(state, dispute_id)
            cache["pb"] = dsp.load_playbook(case["triage"]["reason"], any(i["kind"] == "RECEIPT" for i in ev["evidence"]))
        return cache["pb"]

    async def get_case_facts() -> dict:
        """Triage for this dispute: reason, stage, amount, deadline, fee, Seller Protection, dispute rate,
        receipt strength and the buyer's messages."""
        case = await _case(state, dispute_id)
        raw = (await dbmod.fetchrow(state.pool, "SELECT raw FROM disputes WHERE dispute_id = %s", dispute_id))["raw"]
        return redactor.redact({
            "dispute_id": dispute_id, "triage": case["triage"], "receipt_strength": case["receipt_strength"],
            "buyer": ((raw.get("disputed_transactions") or [{}])[0]).get("buyer"),
            "buyer_messages": raw.get("messages", []),
        })

    async def get_order_evidence() -> dict:
        """Evidence items (ids E1, E2, ...) from PayPal and the intent receipt, plus missing evidence (gaps)."""
        ev = await _evidence()
        return {"evidence": ev["red"], "gaps": ev["gaps"]}

    async def verify_receipt() -> dict:
        """Verification of the intent receipt: hash chain, merchant signature, PayPal custom_id match, strength."""
        ev = await _evidence()
        item = next((i for i in ev["red"] if i["kind"] == "RECEIPT_VERIFICATION"), None)
        return item["data"] if item else {"receipt": None, "note": "This order has no intent receipt."}

    async def compute_economics() -> dict:
        """The deterministic decision (FIGHT / OFFER / ACCEPT / EXPIRED) with fee, deadline and expected values.
        You must follow this decision."""
        return effective_economics(await _case(state, dispute_id))

    async def load_playbook() -> dict:
        """Guidance for this dispute reason plus PayPal policy lines with IDs (POL-...) you may cite."""
        pb = await _playbook()
        return {"text": pb["text"], "policy_ids": sorted(pb["policy_ids"])}

    async def check_draft(draft: dsp.Draft) -> list[str]:
        """Checks a draft against the evidence. Returns the problems; an empty list means it passes."""
        ev, pb = await _evidence(), await _playbook()
        return dsp.check_draft(draft, ev["red"], pb["policy_ids"], effective_economics(await _case(state, dispute_id)))

    async def build_evidence_pack(draft: dsp.Draft) -> dict:
        """Checks the draft, then saves it with its evidence PDF and the exact payload to submit.
        Returns {payload_hash, action} on success, or {problems} to fix."""
        ev, pb = await _evidence(), await _playbook()
        case = await _case(state, dispute_id)
        problems = dsp.check_draft(draft, ev["red"], pb["policy_ids"], effective_economics(case))
        if problems:
            await dbmod.execute(state.pool, "UPDATE cases SET check_issues = %s WHERE dispute_id = %s",
                                Jsonb(problems), dispute_id)
            return {"problems": problems}
        real = dsp.Draft.model_validate_json(redactor.restore(draft.model_dump_json()))
        return await persist_draft(state, dispute_id, real, ev["evidence"], pb, case)

    async def submit_dispute_response(action: str, payload_hash: str) -> dict:
        """Submits the seller-approved response to PayPal. Pauses for the seller's approval before it runs."""
        return await dsp.submit_approved(state.pool, state.pp, dispute_id, action, payload_hash)

    fns = (get_case_facts, get_order_evidence, verify_receipt, compute_economics, load_playbook,
           check_draft, build_evidence_pack, submit_dispute_response)
    # The submit tool returns directly: once PayPal has the response there is nothing left for the model to do,
    # so the seller's Approve does not wait for another LLM round trip.
    return [StructuredTool.from_function(coroutine=f, name=f.__name__, description=f.__doc__,
                                         return_direct=f is submit_dispute_response) for f in fns]


def build_agent(state, dispute_id: str, model=None):
    tools = make_tools(state, dispute_id, Redactor())
    by_name = {t.name: t for t in tools}
    fact_checker = {"name": "fact-checker", "description": "Checks a dispute draft against the evidence and lists unsupported claims.",
                    "system_prompt": FACT_CHECKER_PROMPT,
                    "tools": [by_name[n] for n in ("get_order_evidence", "verify_receipt", "check_draft")]}
    return create_deep_agent(
        model=model or chat_model(state.settings), tools=tools, system_prompt=SYSTEM_PROMPT, subagents=[fact_checker],
        interrupt_on={SUBMIT_TOOL: {"allowed_decisions": ["approve", "edit", "reject"]}},
        checkpointer=state.checkpointer,
    )


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}


async def _fail(state, dispute_id: str, exc: Exception) -> str:
    await dbmod.execute(state.pool, "UPDATE cases SET state = 'NEEDS_ATTENTION', error = COALESCE(error, %s), "
                        "updated_at = now() WHERE dispute_id = %s", repr(exc)[:500], dispute_id)
    return "NEEDS_ATTENTION"


async def _after(state, dispute_id: str, result: dict, rejected: bool) -> str:
    interrupts = result.get("__interrupt__")
    if interrupts:
        pending = interrupts[0].value["action_requests"][0]
        case = await _case(state, dispute_id)
        if not case["draft"] or case["payload_hash"] != (pending.get("args") or {}).get("payload_hash"):
            # The model asked to submit something the seller cannot review: never show it as awaiting approval.
            await dbmod.execute(state.pool, "UPDATE cases SET state = 'NEEDS_ATTENTION', error = %s, updated_at = now() "
                                "WHERE dispute_id = %s", "agent asked to submit without a saved, checked draft", dispute_id)
            return "NEEDS_ATTENTION"
        await dbmod.execute(state.pool, "UPDATE cases SET state = 'AWAITING_APPROVAL', pending_action = %s, updated_at = now() "
                            "WHERE dispute_id = %s", Jsonb(pending), dispute_id)
        return "AWAITING_APPROVAL"
    case = await _case(state, dispute_id)
    if case["state"] != "ANALYZING":  # the submit tool already moved it (SUBMITTED / ACCEPTED / NEEDS_ATTENTION)
        return case["state"]
    new = "MANUAL" if rejected else "NEEDS_ATTENTION"
    await dbmod.execute(state.pool, "UPDATE cases SET state = %s, error = COALESCE(error, %s), updated_at = now() WHERE dispute_id = %s",
                        new, None if rejected else "agent finished without a submission", dispute_id)
    return new


async def run_case(state, dispute_id: str, force: bool = False, model=None) -> str:
    allowed = ("state NOT IN ('SUBMITTED', 'WON', 'LOST', 'ACCEPTED', 'ANALYZING')" if force
               else "state IN ('NEW', 'NEEDS_ATTENTION')")
    thread_id = f"{dispute_id}:{uuid.uuid4().hex[:8]}"
    started = await dbmod.fetchrow(
        state.pool,
        f"UPDATE cases SET state = 'ANALYZING', thread_id = %s, error = NULL, pending_action = NULL, check_issues = NULL, "
        f"draft = NULL, payload = NULL, pdf = NULL, pdf_sha256 = NULL, payload_hash = NULL, updated_at = now() "
        f"WHERE dispute_id = %s AND {allowed} RETURNING dispute_id", thread_id, dispute_id)
    if not started:  # another run owns it, or it is finished: never two runs per case
        return (await _case(state, dispute_id))["state"]
    try:
        agent = build_agent(state, dispute_id, model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": f"Handle PayPal dispute {dispute_id}."}]},
                                     config=_config(thread_id))
        for _ in range(2):  # reasoning models sometimes return an empty reply mid-case; nudge instead of giving up
            last = (result.get("messages") or [None])[-1]
            if result.get("__interrupt__") or not isinstance(last, AIMessage) or last.content or last.tool_calls:
                break
            result = await agent.ainvoke({"messages": [{"role": "user", "content": NUDGE}]}, config=_config(thread_id))
        return await _after(state, dispute_id, result, rejected=False)
    except Exception as exc:  # LLM errors, tool errors, rate limits: never leave a case stuck in ANALYZING
        return await _fail(state, dispute_id, exc)


async def resume_case(state, dispute_id: str, decision: dict, model=None) -> str:
    row = await dbmod.fetchrow(state.pool, "UPDATE cases SET state = 'ANALYZING', updated_at = now() "
                               "WHERE dispute_id = %s AND state = 'AWAITING_APPROVAL' RETURNING thread_id", dispute_id)
    if not row:
        return (await _case(state, dispute_id))["state"]
    try:
        agent = build_agent(state, dispute_id, model)
        result = await agent.ainvoke(Command(resume={"decisions": [decision]}), config=_config(row["thread_id"]))
        return await _after(state, dispute_id, result, rejected=decision.get("type") == "reject")
    except Exception as exc:
        return await _fail(state, dispute_id, exc)
