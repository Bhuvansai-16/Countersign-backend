# Handoff: Countersign backend (for the next agent, e.g. Codex)

Last updated 2026-11-05 (evening) by Claude. Read this first, then `docs/design.md` (what and why) and `docs/plan.md`
(task-by-task plan). Every decision taken during the build is in `docs/build-ledger.md` (lines starting `Ruling:`).
Hackathon deadline: **Nov 12, 2026 12:00 PT = Nov 13 01:30 IST**; target submit Nov 12 20:00 IST.

## Status

Branch `build/backend` (not merged to `main`, not pushed). 154 unit tests green (`pytest`), plus an LLM eval
(`pytest -m llm`) in progress.

| Plan task | State |
|---|---|
| 1-3 skeleton, DB schema, PayPal client | done |
| 4 sandbox spikes S1-S7, S9 | **not done**: blocked on a US sandbox app (see below). Only `scripts/spikes/common.py`, `s8_llm.py`, `s8_deepagent.py` exist; write s1-s7 from `docs/plan.md` Task 4 |
| 5-7 receipts, mandates/checkout, HTTP routes | done (plus security-review fixes) |
| 8 PII redaction + LLM client | done; S8 passed on Sarvam |
| 9 MCP server + buyer agent | done; buyer agent verified live on Sarvam over HTTP MCP |
| 10-12 economics (12 golden decisions), dispute core, evidence PDF + QR | done |
| 13 Deep Agent (tools, fact-checker subagent, interrupt_on, Postgres checkpointer) | done |
| 14-15 case/approval routes, webhooks, poller, sweeper | done |
| 16 simulator, judge demo (pool + dry run), seed/reset scripts | done except **live demo mode** (needs spike S3) |
| 17 report_issue agent-to-agent resolution | done |
| 18 AP2 export | done |
| 19 LLM golden eval | **in progress**: harness in `tests/test_eval_llm.py`; G1 PASSED on sarvam-105b (4 min/case). Run the rest |
| 20 deploy (Render + Neon), README, webhook registration, E2E | **not done** |
| Frontend (`../Countersign` repo) | **pages built** (login, confirm, cases queue, case detail, orders, issues, sandbox); type-check and lint clean; confirm page verified live (real sandbox order with our custom_id). See `../Countersign/FRONTEND.md` |

## Run it (Windows, this machine)

```bash
# deps live outside OneDrive
export UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend
uv sync
uv run pytest -q                                  # unit tests; embedded Postgres via pgserver, no setup needed
uv run --env-file .env pytest -m llm -v           # real-LLM golden eval (~4 min/case, Sarvam credits)
uv run --env-file .env python scripts/seed.py     # 12 products + 120 simulated history rows (idempotent)
# API: port 8000 is taken by an unrelated system-python process, so use 8010; Windows needs the selector loop
PUBLIC_BASE_URL=http://localhost:8010 uv run --env-file .env uvicorn app.main:build --factory --loop asyncio:SelectorEventLoop --port 8010
PUBLIC_BASE_URL=http://localhost:8010 uv run --env-file .env python scripts/buyer_agent.py "Buy one linen throw in sage under 90 dollars"
```

- Local DBs are embedded Postgres 16 (`pgserver`): tests use `~/.countersign-pg`, dev uses `~/.countersign-pg-dev`.
  After an unclean shutdown the first start can time out during crash recovery; just retry. The dev DB port changes on
  restart: refresh `DATABASE_URL` in `.env` with
  `python -c "import pgserver,pathlib;print(pgserver.get_server(pathlib.Path.home()/'.countersign-pg-dev',cleanup_mode=None).get_uri())"`.
- `.env` (git-ignored) holds: PayPal sandbox creds (**India-region app, must be replaced**), MERCHANT_SIGNING_KEY,
  SESSION_SECRET, ADMIN_PASSWORD, MCP_AGENT_KEYS (`agt_demo:<key>`), DATABASE_URL, SARVAM_API_KEY, LLM_* (Sarvam).
  Never print or commit it.

## Next steps, in order

1. **Owner: US sandbox.** The default sandbox accounts are region IN. In developer.paypal.com (Sandbox): create a US
   Business account and 2-3 US Personal accounts, create an app "Countersign" linked to the US business account, tick
   Customer disputes + Transaction search, then put its client ID/secret into `.env`. Agents must not create accounts or
   type passwords; the owner does this.
2. **Spikes (Task 4).** Write and run `scripts/spikes/s1..s7` exactly as `docs/plan.md` Task 4 describes; results
   append to `docs/verified.md`. Then reconcile: `disputes.ACTION_REL` rel names, the provide-evidence `input` schema in
   `disputes.payload_for`, the `dispute_outcome.outcome_code` values in `webhooks.OUTCOME_STATE`, the tracker PATCH body
   in `mandates.update_tracking`, and the dispute-creation path in `sim.create_dispute` (auth assertion is a guess).
3. **Finish the LLM eval (Task 19):** `uv run --env-file .env pytest -m llm -v` writes `docs/eval-results.md`. Set
   `EVAL_TRACE=<file>` to dump a failing case's full message trace. Target: at least 10/11 pass, 0 PII leaks.
4. **Deploy (Task 20):** `render.yaml` (web service, start `uvicorn app.main:build --factory --host 0.0.0.0 --port $PORT`),
   Neon Postgres (Render free Postgres expires during judging), register the webhook, seed, run the full E2E once with a
   WON outcome. README per plan Task 20 Step 6 (Tools section naming sarvam-105b is required by Devpost).
5. **Frontend** in `../Countersign` (Next.js + AG Grid + PayPal JS SDK v6): case queue, case detail with approve/edit,
   confirm page calling `POST /api/mandates/{id}/confirm` then `POST /api/orders/{id}/capture`, simulator page.
   API contract: FastAPI `/docs` on the running backend.
6. Final whole-branch review, merge `build/backend` into `main`, then `git subtree add --prefix backend` into the
   `Countersign` repo before submitting (Devpost wants all source in the submitted repo).

## Things that bit us (do not re-learn them)

- **Sarvam (vLLM) quirks, fixed in `app/llm.py`:** langchain-openai sends `max_completion_tokens`, which vLLM ignores,
  so the default 2048 tokens were spent on hidden reasoning and replies came back empty. We send plain `max_tokens`
  through `extra_body` (`LLM_MAX_TOKENS`, default 8192). Tool message content must be a non-empty plain string.
- **Windows:** psycopg async needs the selector event loop (tests use it via `anyio_backend`; uvicorn needs
  `--loop asyncio:SelectorEventLoop`). Render runs Linux, so no flag is needed there.
- **mcp 2.x:** `FastMCP` was renamed `MCPServer`; we mount it as a Starlette `Route` at `/mcp` behind agent-key auth.
- **LangGraph checkpoints:** read messages with `agent.aget_state(config).values["messages"]`, not from raw
  checkpoint tuples.
- **Security reviews** ran on each commit and found 8 issues, all fixed with tests (currency, capture verification, key
  pinning, forged confirmation, cross-agent MCP access, fail-open receipt verification, AP2 signing oracle).
- The approval gate is enforced twice (Deep Agents `interrupt_on` plus `disputes.submit_approved` checking an
  unconsumed approval row with a matching payload hash). Keep both.
