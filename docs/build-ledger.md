# SDD ledger — plan: docs/plan.md
Spec: docs/design.md
Setup: Ruling: work on branch build/backend in the user's checkout, no worktree — repo has no commits and is dedicated to this build — cost if wrong: none (branch merges into main at finish).
Setup: Ruling: uv venv lives outside OneDrive (UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend) for my commands — OneDrive sync/locks on thousands of venv files — cost if wrong: none; README keeps plain `uv sync`.
Setup: Ruling: sandbox default accounts are region IN; owner asked to create US business + US personal accounts and a US-linked app; .env temporarily holds the IN app's sandbox creds — spikes (Task 4) wait for the US app — cost if wrong: spike results from IN app would not match US behaviour.
Pre-flight: T6<-T2/T3/T5 names match (create_order, append_event, sign, seal_payload, custom_id_for) — ok
Pre-flight: T7<-T5/T6 verify_receipt, public_key_b64, MandateError — ok
Pre-flight: T11<-T10 CaseInputs/decide keys (decision, offer, evidence_type) used by check_draft — ok
Pre-flight: T12/T13/T14<-T11 Draft model — ok
Pre-flight: T13<-T8 chat_model(settings, fast) — ok
Pre-flight: T17<-T8 brief writes chat_model(fast=True) without settings — Ruling: call chat_model(state.settings, fast=True) — typo in brief — cost if wrong: none
Task 1: Ruling: Settings also carries demo_dispute_pool and LLM_FAST_MODEL falls back to LLM_MODEL — both needed later, avoids a second config edit — cost if wrong: none
Task 1: complete (commits ad8cf2b..6dba251, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 4 passed in 0.78s)
Task 2: Ruling: db fixture falls back to an embedded Postgres 16 (pgserver dev dependency, data in ~/.countersign-pg) when TEST_DATABASE_URL is unset — no Docker/Neon yet, so tests would otherwise skip and prove nothing — cost if wrong: one dev dependency
Task 2: Ruling: uvicorn entry is the factory app.main:build (--factory) instead of a module-level app — module-level load_settings() would break test imports — cost if wrong: one start-command flag
Task 2: complete (commits 6dba251..af0868c, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 7 passed in 1.61s)
Task 3: live sandbox smoke (IN app): token ok (ttl 32400 s), GET /v1/customer/disputes ok (0 items)
Task 4: Ruling: deferred until the owner's US sandbox app exists; Tasks 5, 8, 10, 12 run first (no spike dependency), Tasks 6/7 use documented PayPal shapes and get re-checked against docs/verified.md — cost if wrong: small fixes in Tasks 6/7 after spikes
Task 3: complete (commits af0868c..c1a852c, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 15 passed in 2.12s)
Task 5: Ruling: verify_receipt takes keyword merchant_name and receipts add iso_z() — the signature covers the merchant name and created_at, which the brief's signature had no way to pass — cost if wrong: none (callers pass settings.merchant_name)
Task 5: complete (commits c1a852c..ef226ba, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 27 passed in 2.31s)
Task 6: Ruling: an invalid agent signature is rejected (401) rather than stored as unsigned — a forged signature should not create a receipt — cost if wrong: agents with broken signing get an error instead of a weaker receipt
Task 6: Ruling: PayPal-Request-Id for order/capture/track is uuid5 of the mandate/order id, and confirm resumes a CONFIRMED-without-order receipt — retries after a lost response stay idempotent at PayPal — cost if wrong: none
Task 6: Ruling: quote carries item_total (needed for the PayPal amount breakdown) — cost if wrong: none
Task 6: complete (commits ef226ba..cbe7f94, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 38 passed in 8.02s)
Task 7: security review fixed — non-USD mandate 422 (test_non_usd_currency_rejected), capture must be COMPLETED and match stored amount (test_capture_not_completed_does_not_mark_captured, test_capture_amount_mismatch_rejected), pinned agent must sign (test_pinned_agent_must_sign); all RED→GREEN, suite 50/50
Task 7: Ruling: no nonce/timestamp in the agent-signed body — a replayed signed mandate still needs a fresh human confirmation and PayPal approval — cost if wrong: duplicate pending mandates from a replay
Task 7: Ruling: /api/orders computes receipt strength against the stored custom_id (no PayPal call per row); /verify and /api/receipts/{id}/verify check live against PayPal — cost if wrong: grid could show STRONG for an order whose PayPal custom_id was altered (not possible via API)
Task 7: complete (commits cbe7f94..dc3c2ac, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 50 passed in 9.15s)
Task 8: Ruling: plain-string "name" is redacted only under buyer/payer/seller/customer/recipient parents and the phone regex needs separators — items[].name (products) and tracking numbers are evidence and must reach the drafter intact — cost if wrong: an unusual PII field slips through (tests cover PayPal's dispute/order shapes)
Task 8: S8 part 1 script written; not run yet — no LLM key in .env (owner action)
Task 8: complete (commits dc3c2ac..1df7acc, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 55 passed in 10.96s)
Task 8: security review fixed — public confirm cannot choose agent_relayed (test_public_confirm_cannot_choose_agent_relayed RED→GREEN, suite 56/56). Ruling: hosted click stays a weak signal (an agent with the URL could click); PayPal buyer login is the strong human proof; README must say so; passkey on the confirm page is the upgrade — cost if wrong: judges question the BUYER_CONFIRMED evidence
Task 9: Ruling: mcp 2.x — MCPServer (FastMCP was renamed), mounted as a Starlette Route at /mcp via an ASGI endpoint that checks the agent key and sets a ContextVar; SDK localhost Host check disabled because every request needs an agent key — cost if wrong: none
Task 9: manual check: uvicorn (--loop asyncio:SelectorEventLoop on Windows) + mcp Client over HTTP: 5 tools listed, create_mandate ok, receipt agent id = agt_demo from the key (ContextVar crosses HTTP). buyer_agent.py written, not run (no LLM key yet)
Task 9: complete (commits 2b6646c..4348fe8, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 62 passed in 16.03s)
Task 10: complete (commits 4348fe8..24b33e5, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 79 passed in 18.33s)
Task 10/11 gap: security review fixed — MCP mandate tools scoped to the calling agent (test_other_agent_cannot_read_or_cancel_mandate RED→GREEN, suite 80/80)
Task 11: Ruling: gather_evidence(pool, pp, dispute_id, settings) and payload_for(draft) — evidence needs settings for receipt verification; payloads need nothing from settings — cost if wrong: none
Task 11: Ruling: check_draft receives the effective economics (override applied by the caller) instead of reading cases.override itself — keeps it pure — cost if wrong: none
Task 11: Ruling: evidence starts with a DISPUTE item (E1) so the dispute ID, dates and amount in drafts verify against evidence — cost if wrong: none
Task 11: Ruling: dispute JSON fixture follows PayPal's documented Disputes v1 shape (rel names provide_evidence/make_offer/accept_claim, seller_transaction_id); re-check against docs/verified.md after spike S4 — cost if wrong: rename rels in ACTION_REL
Task 11: complete (commits 24b33e5..48d560c, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 96 passed in 20.58s)
Task 12: Ruling: merchant name travels in case['merchant_name'] (signature had no slot); PDF built with invariant=1 so identical input gives identical bytes — cost if wrong: none. Visual check of a sample page done.
Task 12: complete (commits 48d560c..419dbb1, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 100 passed in 22.30s)
Task 12/13 gap: security review fixed — receipt verification fails closed when PayPal is unreachable (test_receipt_not_strong_when_paypal_unreachable RED→GREEN, suite 101/101)
Task 13: Ruling: run_case/resume_case/build_agent take an optional model= for tests (scripted model reads payload_hash from the previous tool result) — cost if wrong: none
Task 13: Ruling: tools are bound to one dispute (no dispute_id argument) and built per run; a new run clears the old draft/payload/hash; force re-run never touches SUBMITTED/WON/LOST/ACCEPTED/ANALYZING — cost if wrong: none
Task 13: Ruling: rejected-and-finished runs end MANUAL; finished-without-submission runs end NEEDS_ATTENTION — cost if wrong: none
Task 13: S8 part 2 script written; not run yet (no LLM key). deepagents 0.7.32 interrupt shape confirmed with a scripted model: {action_requests:[{name,args,description}], review_configs}
Task 13: complete (commits b03c123..b9aadd7, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 110 passed in 34.59s)
Task 14: Ruling: case routes live in app/cases_api.py (api.py would pass 400 lines) — cost if wrong: none
Task 14: Ruling: POST /reject sets MANUAL without resuming the agent (no LLM call needed to stop) — cost if wrong: the paused thread is abandoned, which is the intent
Task 14: Ruling: seller draft edits are checked against the real (unredacted) evidence and saved through the shared agent.persist_draft — cost if wrong: none
Task 14: complete (commits b9aadd7..af2592d, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 120 passed in 86.31s (0:01:26))
Task 15: Ruling: outcome codes mapped explicitly (SELLER_FAVOUR/CANCELED_BY_BUYER/DENIED -> WON; BUYER_FAVOUR/ACCEPTED/RESOLVED_WITH_PAYOUT -> LOST) instead of the brief's substring test, which would score CANCELED_BY_BUYER as LOST — cost if wrong: a new PayPal code leaves state unchanged (outcome still stored)
Task 15: Ruling: background loops start 10 s after boot, tick every 60 s (poll 300 s, sweep 600 s); one failing job is logged, never fatal — cost if wrong: none
Task 15: complete (commits af2592d..f25722c, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 131 passed in 98.00s (0:01:37))
Task 16: Ruling: run after Tasks 17-18 — its dispute-creation path depends on spike S3 — cost if wrong: none
Task 17: Ruling: report_issue/respond/get take agent_id and check it owns the receipt (security); issue_type OTHER also gets the 40% partial offer — cost if wrong: none
Task 17: Ruling: the summary is part of the proposal (so it is covered by the approval hash); no LLM call when LLM_MODEL is unset — cost if wrong: none
Task 17: complete (commits f25722c..6456bc9, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 141 passed in 107.66s (0:01:47))
Task 8/13: S8 PASS on sarvam-105b (https://api.sarvam.ai/v1): JSON mode ok; Deep Agent tool call + interrupt_on + AsyncPostgresSaver resume ok. Ruling: Sarvam is the default LLM in .env (owner request, 958 credits) — cost if wrong: switch LLM_* back to another provider
Task 18: Ruling: AP2 fields from the repo's models/mandate.py (IntentMandate/CartMandate/PaymentMandate), not the newer generated checkout_mandate schema; JWT claims iss/sub/aud/iat/exp/jti/cart_hash per its docstring plus seal_hash; amounts are floats as in AP2's PaymentCurrencyAmount; user_authorization null (PayPal holds the buyer's authorisation) — cost if wrong: field renames if AP2's newer schema is preferred
Task 18: tests conftest retries embedded Postgres start (crash recovery after unclean shutdown can exceed pgserver's 10 s)
Task 18: complete (commits 6456bc9..61e8deb, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 144 passed in 56.40s)
Task 18 follow-up: security review fixed — AP2 JWT only for verified sealed receipts (test_ap2_no_merchant_signature_for_unsealed_or_tampered_receipt) and signed cart from chained events (test_ap2_cart_comes_from_chained_events_not_mutable_columns), both RED→GREEN, suite 151/151
Task 16: Ruling: simulator actions find PayPal's link by href suffix (/require-evidence, /adjudicate, /escalate) since rel names for sandbox actions are unverified — still PayPal-provided URLs — cost if wrong: none
Task 16: Ruling: live demo mode (card order -> chargeback/assertion dispute) waits for spike S3; pool + dry-run modes shipped now; dry run resets only DEMO_DISPUTE_POOL cases — cost if wrong: judge demo needs pre-created disputes
Task 16: seed.py run twice on dev DB (idempotent: 12 products, 120 history rows); demo_reset.py ran clean
Task 16: complete (commits 61e8deb..7a9c4d5, tests: env UV_PROJECT_ENVIRONMENT=C:/Users/Bhuvansai/.venvs/countersign-backend uv run pytest -q → 151 passed in 62.63s (0:01:02))
Task 9 follow-up: buyer_agent.py verified live on sarvam-105b over HTTP MCP (port 8010; 8000 is held by an unrelated system-python process left untouched): search -> create_mandate under cap -> confirm link, no payment
Task 19: in progress — root causes fixed with tests: (1) langchain-openai sends max_completion_tokens which vLLM/Sarvam ignores -> 2048 default eaten by reasoning -> empty replies; now extra_body max_tokens (LLM_MAX_TOKENS=8192); (2) tool content must be a non-empty string for vLLM. G1 PASSED on sarvam-105b (247 s). Remaining G2-G11 not run yet.
HANDOFF: session ending at owner's usage limit; see docs/HANDOFF.md
