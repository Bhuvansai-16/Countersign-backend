# Handoff: Countersign backend (for the next agent, e.g. Codex)

## November 7 remaining plan

The public receipt now shows the recorded cart, capture time, plain-language checks and an activity timeline. Technical details hold the seal, PayPal commitment, event hashes and public key. The existing $60 sandbox receipt verified STRONG against PayPal. The dispute evidence PDF already includes a QR code for this verifier when the case has a receipt. AP2 export is also implemented.

1. **Live sandbox verification (Task 4).** Use an existing captured sandbox order to verify tracking. Walk a real dispute through intake, evidence submission or offer, and resolution. Confirm the PayPal action links, payloads and outcome codes against those responses. The judge demo already supports pooled cases and a labelled dry run.
2. **Live evaluations (Task 19).** Run the remaining golden cases and record decisions, grounding checks, PII checks and costs. The recorded live result covers G1.
3. **Hosted release (Task 20).** Add deployment configuration, host the API and database, configure and verify the webhook, seed the hosted sandbox, and run the hosted buyer-to-dispute flow. Set `PUBLIC_BASE_URL` to the hosted verifier so the evidence QR works on another device. Finish the submission README, source packaging and demo recording.

See the frontend's `FRONTEND_PLAN.md` for the merchant UI walkthrough and the remaining original frontend scope.

## November 6 checkpoint

Public frontend entry is now `/` landing → `/shop`, with no shopper login. Shopping endpoints use signed, expiring HttpOnly visitor cookies and per-visitor candidate ownership; merchant APIs still require seller authorization. Anonymous requests with unrelated Origin headers are rejected. Provider credentials remain server-side, and the shopping feature remains sandbox-gated.

Verification: 171 backend tests passed, 11 live LLM evaluations deselected; final shopping-only rerun: 16 passed. Frontend production build, TypeScript, ESLint and seven Node tests pass. Browser checks covered anonymous real search, selection, an $80.97 unconfirmed quote, desktop/mobile layouts, and the existing $85.95 captured receipt's verified return/acknowledgement, script-opened tab close and blocked-close redirect. No new payment or deployment. Development frontend was restarted after a stale layout render; a fresh merchant redirect and public shopping entry showed no browser errors. See frontend `FRONTEND_PLAN.md` for the current UI handoff. Older checkpoints below are historical.

Last updated 2026-11-05 by Codex (US sandbox app connected; see verification checkpoint below). Read this first, then `docs/design.md` (what and why) and `docs/plan.md`
(task-by-task plan). Every decision taken during the build is in `docs/build-ledger.md` (lines starting `Ruling:`).
Hackathon deadline: **Nov 12, 2026 12:00 PT = Nov 13 01:30 IST**; target submit Nov 12 20:00 IST.

## Status

Branch `build/backend` (not merged to `main`, not pushed). 155 unit tests green (`pytest`), plus an LLM eval
(`pytest -m llm`) in progress.

| Plan task | State |
|---|---|
| 1-3 skeleton, DB schema, PayPal client | done |
| 4 sandbox spikes S1-S7, S9 | **incomplete**: US app OAuth, buyer confirmation, PayPal approval, capture and STRONG receipt verification pass. Tracking and the dispute lifecycle remain unverified. Only `scripts/spikes/common.py`, `s8_llm.py`, `s8_deepagent.py` exist; write the remaining spikes from `docs/plan.md` Task 4 |
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
- `.env` (git-ignored) holds: PayPal sandbox creds (**US Countersign app, replaced Nov 5**), MERCHANT_SIGNING_KEY,
  SESSION_SECRET, ADMIN_PASSWORD, MCP_AGENT_KEYS (`agt_demo:<key>`), DATABASE_URL, SARVAM_API_KEY, LLM_* (Sarvam).
  Never print or commit it.

## Next steps, in order

Shopping demo added Nov 5: see `docs/SHOPPING_DEMO.md`. `/shop` provides chat, real Channel3 cards and a Kernel
live view inside the seller console. Backend-only provider keys are configured locally and `DEMO_ENABLED=true`.
Live search, browser execution and unpaid cart validation pass; suite is **169 passed**, 11 live LLM evaluations
deselected. New `/api/demo/shopping/*` endpoints and four external MCP tools are sandbox gated. Imported products
are explicitly labelled sandbox copies, never purchases from source retailers. Brave live chat search, numbered
follow-up and $85.95 cart preparation pass; `rcpt_6ee09dc4ef0996cbef375500` is still unpaid with no PayPal order.
Kernel video rendered separately; the initial embedded view was blank, so use Open browser separately if needed.
The tested source buy link displayed a security check; recognized checks now return an inspection error.

1. **Continue from the captured US order.** The US Business account `sb-iaenv53656392@business.example.com` and two US Personal
   accounts now exist. The "Countersign" merchant app is linked to that business account; Customer disputes and
   Transaction search are enabled and its credentials are in the ignored `.env`. API restarted on 8010; frontend on
   3000. `http://localhost:3000/confirm/rcpt_68bf79004ca5cdd170749995` now shows **Paid. Thank you!** ($84 sage throw,
   cap $90). The owner approved as Buyer A `sb-v3mdl53715004@personal.example.com`. US order `2NG228202S383952F` is
   **COMPLETED**, capture `3L906348KK120212Y`; live receipt verification is **STRONG**, all four integrity checks true.
   Repeating capture returns the same capture ID. Buyer B `sb-pprgt53715166@personal.example.com` remains in reserve.
   Next: add tracking and verify the sandbox dispute lifecycle. No buyer passwords were read or changed. Old
   India-app orders remain in the DB and may be unreadable with the new app. PowerShell runbook: `docs/RUN_LOCAL.md`.
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

- **Live capture fix (Nov 5):** PayPal rejected the empty capture POST with UNSUPPORTED_MEDIA_TYPE because httpx did
  not send Content-Type. `capture_order` now sends `json={}`; the new header regression test failed before the fix.
  Final full suite: **155 passed, 11 LLM deselected**. The first full run had 87 test-DB setup errors; reconnecting the
  existing test PostgreSQL after recovery resolved them without a fixture/code change.
- **Next.js dev worker:** `/login` loaded but `/confirm/[id]` returned 500 (failed static-path worker, EPIPE).
  Restarting only this project's frontend restored the same return URL to 200; no frontend source changes.

- **Nov 5 US setup checkpoint:** 154 backend tests pass (11 live LLM cases deselected); frontend type-check and lint
  pass with `node node_modules/typescript/bin/tsc --noEmit` and `node node_modules/eslint/bin/eslint.js .`.
  The shell's `npm`/`npx` shims point at missing roaming files, so use the installed project executables directly.
  The dev database crashed and recovered on port 50168; `.env` was updated to that port without clearing data.
  API `/healthz` reports DB healthy; products, cases, orders, issues and metrics return 200. Live mandate validation
  rejects OVER_CAP and ATTRIBUTE_MISMATCH (422). PayPal OAuth and seller dispute list return 200 (0 disputes).

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
