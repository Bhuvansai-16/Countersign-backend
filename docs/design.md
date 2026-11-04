# Countersign backend: design

Status: draft for approval · 2026-11-04 · Owner: Bhuvansai
Source plan: `../PayPal AI hackathon build plan.md` (called "the plan" below; section numbers refer to it).
Frontend lives in the separate `Countersign` repo and is built after this backend.

---

## 1. What this is and why

Countersign (renamed from IntentProof) is a dispute-defense backend for small PayPal sellers in the age of AI shopping agents.

- **At checkout:** a buyer's AI agent can *quote* through our MCP server but cannot *pay*. A human confirms on a hosted page, PayPal approves, and every step lands in a hash-chained, Ed25519-signed **intent receipt**. The receipt's seal hash is written into the PayPal order's `custom_id` **before payment**, so PayPal's own record carries the commitment and the seller cannot fabricate it later.
- **When a dispute arrives:** a LangChain Deep Agent plans the case, calls deterministic tools to triage it, price fight / offer / accept and gather evidence (order, capture, tracking, receipt), drafts a reason-specific response, has a fact-checker subagent verify every claim, builds a PDF, and then calls its submit tool, which pauses for the seller's approval. After approval it submits the exact approved payload through the dispute's own HATEOAS link.
- **Before a dispute (add-on):** the buyer's agent can report a problem through MCP; the seller resolves it with a refund or replacement, avoiding the dispute fee and the dispute-rate hit.

**Positioning (from research on 2026-11-04):** do not claim "first evidence layer". Mastercard + Google published *Verifiable Intent* (Mar 2026, open SD-JWT spec) and Justt / Chargebacks911 market agent-purchase chargebacks. Countersign's pitch is **"Verifiable-Intent-style proof, native to PayPal disputes"**: the commitment lives inside PayPal's order record, receipts export in AP2 mandate shape, and it closes the PayPal Agent Toolkit's write gap (toolkit has only `list_disputes`, `get_dispute`, `accept_dispute_claim`; no provide-evidence or make-offer).

## 2. Goals, non-goals, anti-goals

**Goals (backend done by end of Nov 8 IST):**
1. Full sandbox loop works via API: agent mandate → human confirm → PayPal approve → capture → track → dispute → draft → approve → submit → outcome.
2. Receipt verification is independently checkable (public verify endpoint + public key + `custom_id` comparison against PayPal).
3. Clean, documented HTTP API that the frontend can build against (FastAPI `/docs`).
4. Add-ons: AP2 mandate export, `report_issue` pre-dispute resolution, QR verify code on the PDF, one-click judge demo (if the sandbox allows it, see §10).
5. 12 golden eval cases pass deterministically (≥ 11/12 decisions).

**Non-goals:** production go-live, multi-tenant onboarding, Store Sync / Agent Ready, implementing the Verifiable Intent SD-JWT format, carrier APIs, appeals, chargeback representment beyond the sandbox demo, multi-language drafts, Channel3, vector search.

**Anti-goals (failure even if it "works"):**
- Anything is ever submitted to PayPal without a matching human approval.
- The LLM states a number, date, tracking ID or policy that is not in the evidence.
- Buyer PII is sent to an LLM provider.
- The demo depends on a manual step a judge cannot reproduce, without it being disclosed.
- Overclaiming: receipts are *evidence*, not a liability shift. Agent-dispute volumes are forecast, not measured.

## 3. Constraints

- **Hard deadline** Nov 12 12:00 PT = Nov 13 01:30 IST. Target submit Nov 12 20:00 IST. Frontend still has to be built after the backend, by the same person.
- **PayPal sandbox only.** The dispute lifecycle in sandbox is the main technical risk (§10).
- **LLM budget:** $13.99 on Nebius Token Factory. Development runs on free tiers (Gemini, Groq). Per-case cost must stay in cents.
- **Two repos:** `Countersign-backend` (this) and `Countersign` (frontend). Each needs a public OSI licence (MIT) visible in GitHub About; the frontend README links here and vice versa. Devpost rules say the submitted repo "must contain all source code", so before submission the backend is copied into `Countersign` under `backend/` with `git subtree`, and `Countersign` is the repo submitted.
- **Submission rules (Devpost, checked 2026-11-04):** a hosted demo URL *or* complete run instructions; a "Tools" section naming every AI model and how it is used; a **public** YouTube video under 3 minutes.
- **Hosting must stay up through judging (Dec 1–21).** Render free Postgres expires after ~30 days and free web services sleep (**VERIFY** current Render terms); use sponsor credits for paid instances, or Neon free Postgres as fallback.

## 4. Stack (decided)

| Concern | Choice | Why |
|---|---|---|
| Language / runtime | Python 3.12, `uv` for deps | Plan's choice; fast setup |
| Web | FastAPI + uvicorn | Async, free OpenAPI docs for the frontend |
| PayPal | Raw REST via `httpx.AsyncClient` | All writes need raw Disputes v1 anyway; avoids toolkit's `langchain` pin |
| DB | Postgres 16 via `psycopg` 3 (async, `dict_row`), one `schema.sql` applied at startup | No ORM, no Alembic |
| Crypto | `cryptography` (Ed25519), stdlib `hashlib`, `hmac`, `base64` | No PyJWT needed for one JWS |
| MCP server | Official `mcp` Python SDK (FastMCP), streamable HTTP mounted at `/mcp` | Buyer-agent tools |
| Agent framework | `deepagents` (LangChain, built on LangGraph) + `langgraph-checkpoint-postgres` | Owner's choice. Planning, subagents, and `interrupt_on` human approval; Postgres checkpointer keeps a paused case alive across restarts |
| LLM | `langchain-openai` `ChatOpenAI` with `base_url` pointed at any OpenAI-compatible endpoint (§8) | One class for Gemini, Groq and Nebius |
| PDF + QR | `reportlab` (has a built-in QR widget) | One dependency |
| Background work | `asyncio` tasks started in FastAPI lifespan | No separate worker service |
| Tracing (optional) | LangSmith, on only if `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are set (LangGraph traces automatically) | Shows the agent's plan and tool calls in the video at near-zero cost |
| Tests | `pytest` + recorded PayPal JSON fixtures | No live PayPal in CI |
| Deploy | Render web service + Postgres (`render.yaml`) | Sponsor prize; webhooks need public HTTPS |

**Removed from the plan, and why:** hand-written LangGraph nodes (Deep Agents provides the loop), pgvector RAG (about 6 short playbooks, returned by the `load_playbook` tool), separate worker service, Alembic, SQLAlchemy, Transaction Search on the critical path (3 h lag), PayPal MCP as the primary read path (kept as an optional extra, §10 S9).

## 5. Architecture

One FastAPI process:

```
app/
  main.py          FastAPI app, lifespan (DB pool, schema.sql, background loops), mounts /mcp
  config.py        reads env vars (os.environ), fails fast on missing required ones
  db.py            pool + tiny query helpers
  schema.sql
  paypal.py        PayPalClient: token cache, request-id, mock header, orders/captures/disputes/webhooks
  receipts.py      canonical JSON, hash chain, seal, sign, verify, strength, AP2 export
  mandates.py      create/confirm mandate, create order, capture, track
  disputes.py      plain functions: ingest, triage, economics, evidence, playbooks, self-check, submit (unit-tested, no LLM)
  agent.py         Deep Agent: model, system prompt, tools wrapping disputes.py, fact-checker subagent, interrupt_on, Postgres checkpointer, run/resume helpers
  issues.py        report_issue pre-dispute flow (add-on)
  llm.py           chat-model factory (ChatOpenAI + base_url), JSON-mode helper, PII redaction
  pdf.py           evidence pack + QR
  mcp_server.py    FastMCP tools
  api.py           HTTP routes (or split by area if it grows past ~400 lines)
  webhooks.py      PayPal webhook receiver
  loops.py         dispute poller, deadline sweeper
corpus/playbooks/  INR.md, SNAD.md, UNAUTHORIZED.md, DUPLICATE.md, CREDIT_NOT_PROCESSED.md, AGENT_INTENT.md
scripts/           make_keys.py, seed.py, buyer_agent.py, demo_reset.py, spikes/*.py
tests/             unit tests + eval fixtures
```

The module split is a starting point; the builder may merge files.

## 6. Data model

Keep the plan's §9 DDL with these changes:
- Drop `policy_chunks`, the `vector` extension, `evidence_items` (store evidence as JSONB on `cases`), and `submissions` (store the response on `approvals`).
- Rename `intent_receipts` → `receipts`, receipt id prefix `rcpt_`, mandate id = receipt id.
- Keep `receipt_events` append-only with the `forbid_mutation` trigger.
- Add `agent_public_key` and `agent_sig` on `receipts` (optional agent signature, §9).
- Add `issues` (add-on): `id, receipt_id, capture_id, type, description, desired_resolution, proposal JSONB, state (OPEN|PROPOSED|APPROVED|RESOLVED|REJECTED), resolution JSONB, created_at`.
- Add `history_txns` for the dispute-rate gauge: `id, at, amount, disputed BOOL, simulated BOOL`. Rows created by seed are `simulated = true` and labelled as such in the UI.
- `approvals.action` adds `REFUND` and `REPLACEMENT` (for issues), and the table gets `issue_id` (nullable) beside `dispute_id`.

Case states: `NEW → ANALYZING → AWAITING_APPROVAL → SUBMITTED → WON | LOST`, plus `ACCEPTED`, `EXPIRED`, `NEEDS_ATTENTION`, `MANUAL`.

## 7. Intent receipt

Exactly the plan's §9 "hashing and signing scheme", with prefix `cs1` instead of `ip1`:

- `canonical(x) = json.dumps(x, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()`; amounts as strings, timestamps RFC 3339 UTC.
- `event_hash_i = sha256(prev_hash_i + canonical({seq,type,at,payload}))`, `prev_hash_0 = "0"*64`.
- Pre-payment events: `MANDATE_CREATED`, `CART_SNAPSHOT`, `BUYER_CONFIRMED` (or `AGENT_RELAYED_CONFIRMATION`). Seal = hash of the last one.
- `merchant_sig = Ed25519(seal_payload)`; public keys at `GET /.well-known/countersign-keys.json`.
- PayPal commitment: `purchase_units[0].custom_id = "cs1:" + seal_hash` (68 chars).
- Post-seal events: `ORDER_CREATED`, `PAYPAL_APPROVED`, `CAPTURED`, `TRACKING_ADDED`, `TRACKING_UPDATED`, `ISSUE_REPORTED`, `ISSUE_RESOLVED`.
- Strength: `STRONG` = hosted click + PayPal approved + sig ok + `custom_id` match; `MEDIUM` = PayPal approved but agent-relayed confirmation; `WEAK` = anything else; `NONE` = order without a receipt.
- Verify = recompute the chain, compare the seal, verify the signature, `GET /v2/checkout/orders/{id}` and compare `custom_id`.

## 8. LLM usage

**Client:** `ChatOpenAI(model=..., base_url=LLM_BASE_URL, api_key=LLM_API_KEY)` from `langchain-openai`, passed to `create_deep_agent(model=...)`. Two model slots: `LLM_MODEL` (the Deep Agent and its subagent) and `LLM_FAST_MODEL` (single-shot calls, currently only the `report_issue` summary; the agent reads buyer messages itself). Switching provider is an env change only. If a provider's OpenAI-compatible endpoint misbehaves with tool calling, fall back to its native LangChain package (`langchain-google-genai`, `langchain-groq`).

| Provider | `LLM_BASE_URL` | Use |
|---|---|---|
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | Free-tier development |
| Groq | `https://api.groq.com/openai/v1` | Free-tier development (Qwen; **VERIFY** the exact ID via `GET /models`, likely `qwen/qwen3.6-27b`) |
| Nebius Token Factory | `https://api.tokenfactory.nebius.com/v1/` | Eval runs and the recorded demo ($13.99 budget) |

**Single-shot calls** use JSON mode (`response_format={"type":"json_object"}`) plus Pydantic validation and one retry. The Deep Agent's draft is a structured tool argument (validated by the tool's Pydantic schema), not free text.

**Token budget:** Deep Agents adds long built-in prompts (planning, filesystem, subagent tools). Develop on Gemini (generous free tier); Groq's free-tier tokens-per-minute limit may be too small for a full agent run. Run evals and the recorded demo on Nebius. Target well under $0.10 per case; LangSmith shows the real number.

**Division of labour:** the LLM plans, chooses which tools to call, writes the narrative, and explains the decision. Deterministic code (exposed as tools) computes every fact and number: fees, deadlines, Seller Protection eligibility, expected value, receipt verification, the self-check, and the PayPal calls. The agent must follow the economics tool's recommendation; to change it, the seller uses override.

**Privacy:** the tools redact before returning anything to the agent: buyer name, email, street address and phone become tokens (`<BUYER_EMAIL_1>`). Only the PDF builder and the submit tool put the real values back. Free tiers may use prompts for training, so this applies even in development.

## 9. Add-ons

1. **AP2 mandate export.** `GET /api/receipts/{id}/ap2` returns the receipt as an AP2-shaped `IntentMandate` (instruction, constraints, expiry, `user_cart_confirmation_required: true`), a `CartMandate` (cart contents + `merchant_authorization` = compact JWS, EdDSA, signed with the merchant key) and a `PaymentMandate` reference to the PayPal order. **VERIFY** field names against the official `google-agentic-commerce/AP2` types before coding; community ports disagree. The README maps receipt fields to Verifiable Intent's layers in prose (no SD-JWT implementation).
2. **Optional agent signature.** `create_mandate` accepts `agent_public_key` + `agent_sig` (Ed25519 over the canonical mandate). The key is pinned on first use per agent ID. A valid signature is recorded in the receipt and shown as "agent-signed".
3. **`report_issue`: agent-to-agent resolution before a dispute.** This matches PayPal's own hackathon inspiration ("two agents negotiating and settling a payment between themselves"). MCP `report_issue(mandate_id, issue_type, description, desired_resolution)` creates an issue. The LLM summarises it; deterministic economics proposes a refund amount or a replacement. The seller approves the offer (same approval-hash rule), which sends it to the buyer's agent. The buyer's agent answers with MCP `respond_to_proposal(issue_id, decision: ACCEPT|DECLINE|COUNTER, counter_amount?)`. One counter round is allowed; a counter goes back to the seller's queue. On ACCEPT, the backend calls `POST /v2/payments/captures/{id}/refund` (allowed before a dispute opens). The buyer's agent polls `get_issue_status`. The receipt gets `ISSUE_REPORTED`, `ISSUE_OFFERED`, `ISSUE_COUNTERED` and `ISSUE_RESOLVED`. If the buyer disputes anyway, the refund becomes `PROOF_OF_REFUND` evidence.
4. **QR code on the PDF cover** pointing at `PUBLIC_BASE_URL/verify/{receipt_id}`.
5. **Judge demo button** `POST /api/demo/run`: creates a card-funded order, captures, tracks and opens a dispute automatically, so a judge sees a fresh case appear. This ships only if spike S3 finds an automatic dispute path. Otherwise the owner pre-creates a pool of open disputes through the buyer UI; each call takes the next unused one. When the pool is empty, the call runs in **dry-run mode**: the full analysis and approval run, but submission is simulated and clearly labelled "dry run" in the response and UI. The README says which mode is live. (Builder's call, made in review.)

## 10. PayPal integration and verify spikes

The endpoint table and gotchas in plan §7 still apply. Changes: Transaction Search is dropped from the critical path, and the PayPal MCP read path is optional.

**Spikes, run first (Nov 5), results written to `docs/verified.md` with real request/response samples:**

| # | Spike | Pass condition | Fallback |
|---|---|---|---|
| S1 | Token → create order (`custom_id` = 68-char `cs1:` value) → open `payer-action` link, approve as sandbox buyer → capture → track | Capture COMPLETED; `GET` order returns the same `custom_id` | none needed |
| S2 | Card-funded order (`payment_source.card`, sandbox test card) with no buyer login | Capture COMPLETED | Seed only via buyer login |
| S3 | Create a dispute automatically: (a) `POST /v1/customer/disputes` with `PayPal-Auth-Assertion` (try unsigned JWT `{"alg":"none"}` / `{"iss":client_id,"email":buyer_email}`), (b) `POST /v2/customer-support/process-chargeback` on a card capture | Dispute ID returned, webhook fires | (c) buyer opens the dispute in the sandbox Resolution Center UI |
| S4 | Wait for `links[]`; `provide-evidence` multipart (`input` JSON + `evidence-file` PDF) | 200 | Show the PayPal response; mark NEEDS_ATTENTION |
| S5 | Reach `UNDER_REVIEW` and `adjudicate` `SELLER_FAVOR` | RESOLVED webhook | Docs say `require-evidence` also needs `UNDER_REVIEW`, contradicting plan step 4. If no path works, show "Submitted" plus the PayPal response and explain simulated adjudication honestly |
| S6 | Webhook delivery to ngrok / Render + `verify-webhook-signature` | `SUCCESS` | Poller loop is a full fallback |
| S7 | `make-offer` and `accept-claim` on a second dispute | 200 | — |
| S8 | Each LLM provider: `GET /models`; a JSON-mode call; a tiny Deep Agent with one tool + `interrupt_on` + Postgres checkpointer, paused and resumed | Tool call made, interrupt raised, resume completes | Use whichever provider passes; native LangChain package if the OpenAI-compatible route fails |
| S9 (optional) | Remote PayPal MCP `get_dispute` with a Bearer access token | Tool returns the dispute | Skip; REST only |

Rules that always hold: re-GET the dispute before every action and use `links[].href`, never a hard-coded URL; `PayPal-Request-Id` on every POST (stored with the row); a partial refund during an open dispute returns `422 TRANSACTION_DISPUTED`, so use make-offer instead.

## 11. Dispute agent (Deep Agents)

**Deterministic first step (no LLM):** when a dispute webhook or poll arrives, code runs **ingest** (GET the dispute; map `disputed_transactions[].seller_transaction_id` → capture → order → receipt, DB first, else read the order's `custom_id`) and **triage** (normalise the reason; `due_at` from `seller_response_due_date`, else create time + 10 days, 3 if high-volume; fee $0 if < $30 or Seller-Protection eligible, else $15, $30 if high-volume; SP eligibility = INR only, tracking DELIVERED, address match, SNAD never; trailing-90-day dispute rate). It stores the result on `cases` so the queue shows deadline and fee even if the agent fails. Then it starts the agent with `thread_id = dispute_id`.

**Main agent** `create_deep_agent(model, tools, system_prompt, subagents=[fact_checker], interrupt_on={"submit_dispute_response": {"allowed_decisions": ["approve", "edit", "reject"]}}, checkpointer=AsyncPostgresSaver)`. Its system prompt states the role, the rule "use only tool outputs as facts", and the expected order of work. It uses the built-in planning (todos) and the virtual filesystem for working notes.

**Tools given to the agent** (all wrap plain functions in `disputes.py`; all redact PII):

| Tool | Kind | Returns |
|---|---|---|
| `get_case_facts(dispute_id)` | read | triage result, dispute summary, buyer messages (redacted) |
| `get_order_evidence(dispute_id)` | read | order, capture, tracking, refunds, issues, each with an evidence ID |
| `verify_receipt(dispute_id)` | read | chain / signature / `custom_id` check and strength |
| `compute_economics(dispute_id)` | deterministic | `FIGHT`/`OFFER`/`ACCEPT`, expected values, offer type and amount, assumptions (plan §8 formulas; priors from config, shown in the UI) |
| `load_playbook(reason)` | read | the playbook for the reason, plus `AGENT_INTENT.md` when a receipt exists; policy lines carry IDs like `POL-INR-1` and a PayPal source URL |
| `check_draft(draft)` | deterministic | every number, date, amount and ID must appear verbatim in the evidence; every section cites at least one ref; every ref exists. Returns unsupported claims |
| `build_evidence_pack(draft)` | deterministic | saves the draft to `cases`, renders the PDF (FIGHT only), returns `pdf_sha256` and `payload_hash` |
| `submit_dispute_response(action, payload_hash)` | **write, gated by `interrupt_on`** | executes only after the seller's decision (below) |

**Fact-checker subagent:** its own clean context, tools `get_order_evidence`, `verify_receipt`, `check_draft`. The main agent hands it the draft; it returns unsupported claims. The main agent redrafts once; if claims remain, the case is flagged and approval stays blocked until the seller edits them.

**Draft shape** (validated by the tool schema): `{action, evidence_type, note ≤ 2000 chars, sections:[{heading, text, refs:[...]}], offer?: {offer_type, amount}}`. For `OFFER`, type and amount must equal `compute_economics`'s output. The PDF contains evidence and only the PayPal policy lines the draft cites; internal playbook text never goes into it.

**Human approval (the pause):**
1. The agent calls `submit_dispute_response`. Deep Agents interrupts; the run pauses, persisted by the checkpointer. Code sets the case to `AWAITING_APPROVAL` and stores the pending action.
2. The seller reviews in the UI. `PUT /api/cases/{id}/draft` edits re-run `check_draft` and `build_evidence_pack` deterministically, producing a new `payload_hash`.
3. `POST /api/cases/{id}/approve {action, payload_hash}` writes an `approvals` row, then resumes the agent with `Command(resume={"decisions":[{"type":"approve"}]})`, or `edit` with the edited arguments, or `reject` (state `MANUAL`).
4. The tool body runs and **checks again on its own**: an unconsumed `approvals` row exists with the same action and `payload_hash`; the stored draft still hashes to it; `check_draft` passes; the dispute is re-fetched and the matching `links[]` action exists. Only then does it POST to PayPal (multipart for provide-evidence and accept-claim, JSON for make-offer), mark the approval consumed, store the response and set `SUBMITTED`.

So the approval is enforced twice: by the framework's interrupt and by the tool itself. The LLM cannot change what was approved, because the tool takes only a hash and reads the payload from the DB.

**After submission:** outcome tracking is plain code: `CUSTOMER.DISPUTE.RESOLVED` / `UPDATED` webhooks or the poller set `WON` / `LOST`. Any tool error or agent failure sets `NEEDS_ATTENTION`; nothing is ever auto-submitted or auto-accepted.

**Background loops:** dispute poller every 5 min (webhook backup); deadline sweeper every 10 min (flags < 48 h; never auto-accepts).

## 12. HTTP API (contract for the frontend)

All JSON. Seller routes need `Authorization: Bearer <session token>` from `/api/login`. Buyer confirm routes are public and keyed by the unguessable mandate ID. MCP needs a per-agent key.

| Method + path | Purpose |
|---|---|
| `GET /healthz` | DB, PayPal token TTL, LLM config |
| `POST /api/login` | `{password}` → `{token}` (HMAC-signed, expiring) |
| `GET /api/products` | Catalog |
| `POST /api/mandates` | Same as the MCP `create_mandate`, for testing |
| `GET /api/mandates/{id}` | Summary for the confirm page (public) |
| `POST /api/mandates/{id}/confirm` | Seal, sign, create the PayPal order → `{paypal_order_id, approve_url}` (public) |
| `POST /api/orders/{order_id}/capture` | Called from the JS SDK `onApprove` (public, but only for orders created from a confirmed mandate) |
| `POST /api/orders/{order_id}/track` | Add tracking |
| `GET /api/orders` | Orders grid with receipt strength and tracking |
| `GET /api/receipts/{id}` · `/verify` · `/ap2` | Receipt, verification (public), AP2 export (public) |
| `GET /.well-known/countersign-keys.json` | Public keys |
| `GET /verify/{receipt_id}` | Small server-rendered HTML verify page (QR target) |
| `POST /webhooks/paypal` | Verified and deduplicated |
| `GET /api/cases` · `GET /api/cases/{id}` | Queue and detail (as plan §10) |
| `POST /api/cases/{id}/analyze` | Start or restart the agent run for the case |
| `PUT /api/cases/{id}/draft` | Edit → new `payload_hash` |
| `POST /api/cases/{id}/approve` | `{action, payload_hash}` → submit |
| `POST /api/cases/{id}/override` | Change the decision |
| `GET /api/cases/{id}/pdf` | Evidence pack |
| `GET /api/issues` · `POST /api/issues/{id}/approve` | Pre-dispute issues (add-on) |
| `GET /api/metrics` | Dispute rate, thresholds, counts, $ defended |
| `POST /api/sim/...` | Sandbox simulator (create dispute, require-evidence, adjudicate), only when `SIMULATOR_ENABLED=true` |
| `POST /api/demo/run` | Judge demo (add-on 5) |

**MCP tools:** `search_products`, `create_mandate`, `get_mandate_status`, `cancel_mandate`, `get_receipt`, `report_issue`, `respond_to_proposal`, `get_issue_status`. Deliberately no pay/confirm tool.

**Before the frontend exists (Nov 5–8):** the order's `return_url` points at `GET /dev/return?token=...`, a tiny backend route (enabled only when `SIMULATOR_ENABLED=true`) that captures and shows the result. That lets the owner test the confirm → PayPal approve → capture path by opening the `payer-action` link in a browser. The real confirm page with the JS SDK v6 button comes with the frontend and uses the same `/confirm` and `/capture` routes.

**Buyer agent for the demo:** `scripts/buyer_agent.py` connects to `/mcp` with the `mcp` client SDK and drives the tools with the same OpenAI-compatible LLM via tool calling. Any MCP client (Claude Desktop, MCP Inspector) also works; the README shows the config.

## 13. Security and guardrails

- No submission without an approval row whose hash matches; an edit voids it. The only write tool is gated by `interrupt_on` and re-checks the approval itself (§11).
- The LLM sees only redacted data. Refunds for `report_issue` and all sandbox simulator calls are plain code behind seller-approved API routes, never agent tools.
- Webhooks: signature verified via PayPal; simulated events accepted only with `ALLOW_SIMULATED_WEBHOOKS=true`; deduplicated by event ID.
- Secrets only in env vars; `.env` is git-ignored; tokens are never logged.
- `receipt_events` is append-only (DB trigger).
- Simulator and demo routes are off unless their flags are set, and they always require the seller token.
- CORS limited to `FRONTEND_ORIGIN`.

## 14. Testing

- **Unit:** receipt chain tamper fails; bad signature fails; `custom_id` mismatch fails; trigger blocks UPDATE/DELETE; mandate over the cap → 422; fee, deadline and high-volume rules; EV decisions; self-check catches an invented number; submit without approval raises; edit voids approval; PII redaction leaves no email/phone/address.
- **PayPal client:** token refresh on 401; request-id reuse on retry; `PayPal-Mock-Response` `INSTRUMENT_DECLINED` path; dispute ID `ERRDIS015` → FORBIDDEN path.
- **Eval:** the plan's 12 golden cases (§8) as recorded fixtures. Decision (`compute_economics`) and evidence checks are deterministic and run in normal `pytest`. Full agent runs (decision followed, required evidence cited, zero unsupported claims after the fact-checker, interrupt raised before any write) run with a real LLM under `pytest -m llm` (Nebius), results written to `docs/eval-results.md`.
- **Approval gate:** calling the submit tool body directly without an approval row raises; a mismatched hash raises; resuming with `reject` never calls PayPal.
- **E2E (manual, sandbox, hosted):** the full loop once with a WON outcome and once with an offer.

## 15. Environment variables

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_BASE_URL`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_SANDBOX_BUYERS` (JSON), `DATABASE_URL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_FAST_MODEL`, `LANGSMITH_API_KEY` (optional), `MERCHANT_SIGNING_KEY`, `MCP_AGENT_KEYS`, `ADMIN_PASSWORD`, `SESSION_SECRET`, `FRONTEND_ORIGIN`, `PUBLIC_BASE_URL`, `SIMULATOR_ENABLED`, `ALLOW_SIMULATED_WEBHOOKS`, `DEMO_ENABLED`.

## 16. Schedule (IST)

| Day | Backend work | Done when |
|---|---|---|
| Wed Nov 4 (eve) | Sandbox app + accounts (owner); repo skeleton, config, PayPal client, token smoke test | Token call works |
| Thu Nov 5 | Spikes S1–S8; receipts module + tests | `docs/verified.md` written; tamper tests pass |
| Fri Nov 6 | Mandates, confirm, capture, track; MCP server; AP2 export; agent signature; `buyer_agent.py` | Agent → confirm → capture → STRONG receipt |
| Sat Nov 7 | `disputes.py` functions + tests, playbooks, PII redaction, golden fixtures; Deep Agent with read tools + fact-checker | ≥ 11/12 golden decisions; agent produces a checked draft on fixtures |
| Sun Nov 8 | PDF + QR, interrupt/approve/resume + submit tool, webhooks + loops, simulator, `report_issue`, deploy to Render, seed | Hosted loop reaches WON |
| Mon Nov 9 → | Frontend starts (separate repo); backend only gets fixes and the judge demo | — |

## 17. Left to the builder

Exact file split, ID formats, prompt wording, playbook text (one page each), retry/backoff values, logging format, seed catalog content (12 products for a demo store), and the HTML of the small verify page.

## 18. Owner actions (cannot be done by the builder)

1. Create the PayPal sandbox REST app and accounts (one US business seller, five or more US personal buyers), put the credentials in a git-ignored `.env`. Never paste secrets into chat.
2. Approve sandbox payments and, if spike S3 needs it, open disputes as a buyer in the sandbox site.
3. Register the webhook URL in the PayPal dashboard once the backend has a public URL.
4. Claim Render sponsor credits (or create a Neon free database) before Nov 8.
5. Create the LLM keys (Gemini, Groq, Nebius) and put them in `.env`.
