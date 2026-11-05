"""Golden eval with a real LLM: run with `uv run --env-file .env pytest -m llm -v`.

Each case builds a dispute scenario, triages it deterministically, then runs the real Deep Agent. A case passes when
the agent pauses for seller approval with a draft whose action matches the computed decision, the self-check found
no unsupported claims, and no buyer PII reached the model. Results go to docs/eval-results.md.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app import agent as ag
from app import db as dbmod
from app import disputes as dsp
from app import mandates as m
from app.config import load_settings
from app.main import create_app
from tests.conftest import BASE_ENV
from tests.test_disputes import dispute_json, paid_order, pp_for

pytestmark = [pytest.mark.llm, pytest.mark.anyio]
LLM_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_FAST_MODEL")
PII = ("Sam Rivera", "1 Main St", "San Jose")
RESULTS: list[dict] = []

# id: (description, dispute overrides, setup steps, expected decision)
CASES = {
    "G1": ("INR, tracking delivered, Seller Protection", {}, ["delivered"], "FIGHT"),
    "G2": ("INR, no tracking, no receipt", {}, ["no_receipt"], "OFFER"),
    "G3": ("SNAD wrong item, strong receipt, constraints match", {"reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"}, ["delivered"], "FIGHT"),
    "G4": ("SNAD damaged, no receipt", {"reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"}, ["no_receipt"], "OFFER"),
    "G5": ("Unauthorized, strong receipt", {"reason": "UNAUTHORISED"}, [], "FIGHT"),
    "G6": ("Unauthorized, agent-relayed confirmation", {"reason": "UNAUTHORISED"}, ["relayed"], "OFFER"),
    "G7": ("SNAD, agent bought outside the constraints", {"reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"}, ["violation"], "ACCEPT"),
    "G8": ("INR, $12 claim, no receipt", {"dispute_amount": {"currency_code": "USD", "value": "12.00"}}, ["no_receipt"], "ACCEPT"),
    "G9": ("Credit not processed, refund already issued", {"reason": "CREDIT_NOT_PROCESSED"}, ["refunded"], "FIGHT"),
    "G10": ("Duplicate, two distinct orders", {"reason": "DUPLICATE_TRANSACTION"}, ["second_order"], "FIGHT"),
    "G11": ("Deadline passed", {"seller_response_due_date": "2026-11-01T00:00:00.000Z"}, [], "EXPIRED"),
}


@pytest.fixture(scope="module")
def llm_settings():
    if not os.environ.get("LLM_API_KEY"):
        pytest.skip("LLM_API_KEY not set")
    return load_settings({**BASE_ENV, **{k: os.environ[k] for k in LLM_KEYS if os.environ.get(k)}})


@pytest.fixture(scope="module", autouse=True)
def write_results():
    yield
    if not RESULTS:
        return
    model = os.environ.get("LLM_MODEL", "?")
    lines = [f"# LLM eval results\n\nModel: `{model}` via `{os.environ.get('LLM_BASE_URL', '')}`. "
             f"Run: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Command: `uv run --env-file .env pytest -m llm -v`.\n",
             "| Case | Scenario | Expected | Agent action | Self-check issues | PII leak | Main-agent tokens (in/out) | Seconds | Pass |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in RESULTS:
        lines.append(f"| {r['id']} | {r['desc']} | {r['expected']} | {r['action']} | {r['issues']} | {r['pii']} | "
                     f"{r['tok_in']}/{r['tok_out']} | {r['secs']} | {'yes' if r['ok'] else 'no'} |")
    passed = sum(r["ok"] for r in RESULTS)
    lines.append(f"\n**{passed}/{len(RESULTS)} passed.** Token counts cover the main agent only (the fact-checker "
                 f"subagent runs in its own context). Decisions for all 12 golden cases are also checked "
                 f"deterministically in tests/test_economics.py.\n")
    Path("docs/eval-results.md").write_text("\n".join(lines), encoding="utf-8")


async def build_scenario(db, settings, overrides: dict, steps: list[str]):
    dispute = dispute_json(**overrides)
    if "no_receipt" in steps:
        dispute["disputed_transactions"][0]["seller_transaction_id"] = "UNKNOWN-CAPTURE"
    pp = pp_for(dispute)
    if "relayed" in steps:
        await paid_order_relayed(db, settings, pp)
    else:
        await paid_order(db, settings, pp)
    if "delivered" in steps:
        await dbmod.execute(db, "INSERT INTO shipments (paypal_order_id, tracker_id, carrier, tracking_number, status, delivered_at) "
                            "VALUES ('O1', 'C1-9400100000000000000000', 'USPS', '9400100000000000000000', 'DELIVERED', now())")
    if "violation" in steps:
        await dbmod.execute(db, "UPDATE products SET attributes = '{\"color\": \"blue\"}' WHERE sku = 'THROW-SAGE'")
    if "refunded" in steps:
        rid = (await dbmod.fetchrow(db, "SELECT receipt_id FROM orders WHERE paypal_order_id = 'O1'"))["receipt_id"]
        await dbmod.execute(db, "INSERT INTO issues (id, receipt_id, capture_id, type, description, desired_resolution, proposal, "
                            "state, resolution) VALUES ('iss_1', %s, 'C1', 'WRONG_ITEM', 'x', 'REFUND', '{}', 'RESOLVED', "
                            "'{\"type\": \"REFUND\", \"amount\": \"84.00\", \"refund_id\": \"R1\"}')", rid)
    if "second_order" in steps:
        await dbmod.execute(db, "INSERT INTO orders (paypal_order_id, capture_id, status, amount, payer_id, request_id) "
                            "VALUES ('O2', 'C2', 'COMPLETED', 84.00, 'PAYER1', %s)", uuid.uuid4())
    return pp


async def paid_order_relayed(db, settings, pp):
    from tests.test_mandates import _seed_products

    await _seed_products(db)
    md = await m.create_mandate(db, settings, instruction="Buy one linen throw in sage under $90", max_amount="90.00",
                                currency="USD", attributes={"color": "sage"}, items=[{"sku": "THROW-SAGE", "qty": 1}],
                                agent_id="agt_test", agent_platform="custom")
    await m.confirm_mandate(db, pp.client, settings, md["mandate_id"], "agent_relayed", "UA")
    await m.capture(db, pp.client, "O1")


@pytest.mark.parametrize("case_id", list(CASES))
async def test_golden_case(db, llm_settings, case_id):
    desc, overrides, steps, expected = CASES[case_id]
    pp = await build_scenario(db, llm_settings, overrides, steps)
    case = await dsp.triage(db, pp.client, "PP-D-1001", llm_settings)
    assert case["economics"]["decision"] == expected, "scenario no longer produces the expected decision"
    state = create_app(llm_settings, pool=db, paypal=pp.client).state.cs
    state.checkpointer = MemorySaver()
    started = time.monotonic()
    final = await ag.run_case(state, "PP-D-1001")
    secs = round(time.monotonic() - started)
    row = await dbmod.fetchrow(db, "SELECT * FROM cases")
    messages = []
    if row["thread_id"]:
        snapshot = await ag.build_agent(state, "PP-D-1001").aget_state({"configurable": {"thread_id": row["thread_id"]}})
        messages = snapshot.values.get("messages", [])
    sent = json.dumps([getattr(msg, "content", "") for msg in messages] +
                      [getattr(msg, "tool_calls", []) for msg in messages], default=str)
    if os.environ.get("EVAL_TRACE"):  # TEMP debug
        cps = [c async for c in state.checkpointer.alist(None)]
        Path(os.environ["EVAL_TRACE"] + ".meta").write_text(json.dumps({
            "thread_id": row["thread_id"], "n_checkpoints": len(cps),
            "threads": sorted({c.config["configurable"].get("thread_id") for c in cps}),
            "ns": sorted({c.config["configurable"].get("checkpoint_ns") for c in cps}),
            "channels": sorted(cps[0].checkpoint["channel_values"].keys()) if cps else []}, default=str), encoding="utf-8")
        Path(os.environ["EVAL_TRACE"]).write_text("\n\n".join(
            f"[{type(msg).__name__}] tool_calls={json.dumps(getattr(msg, 'tool_calls', None), default=str)[:1500]}\n"
            f"content={str(getattr(msg, 'content', ''))[:1500]}\n"
            f"meta={json.dumps(getattr(msg, 'response_metadata', None), default=str)[:800]}\n"
            f"usage={getattr(msg, 'usage_metadata', None)}\n"
            f"extra={json.dumps(getattr(msg, 'additional_kwargs', None), default=str)[:800]}" for msg in messages), encoding="utf-8")
    pii = [p for p in PII if p in sent]
    tok_in = sum((getattr(msg, "usage_metadata", None) or {}).get("input_tokens", 0) for msg in messages)
    tok_out = sum((getattr(msg, "usage_metadata", None) or {}).get("output_tokens", 0) for msg in messages)
    action = (row["draft"] or {}).get("action", "-")
    issues = len(row["check_issues"] or [])
    if expected == "EXPIRED":
        ok = final == "EXPIRED" and not messages
    else:
        ok = final == "AWAITING_APPROVAL" and action == dsp.DECISION_ACTION[expected] and issues == 0 and not pii
    RESULTS.append({"id": case_id, "desc": desc, "expected": expected, "action": action, "issues": issues,
                    "pii": ", ".join(pii) or "none", "tok_in": tok_in, "tok_out": tok_out, "secs": secs, "ok": ok})
    assert ok, f"{case_id}: final={final} action={action} issues={row['check_issues']} pii={pii} error={row['error']}"
