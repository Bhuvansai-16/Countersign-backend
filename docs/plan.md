# Countersign Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A FastAPI backend that issues signed, PayPal-committed intent receipts for agent purchases and defends PayPal disputes with a LangChain Deep Agent that pauses for seller approval before any write.

**Architecture:** One FastAPI process (REST API + MCP server at `/mcp` + background asyncio loops) over Postgres. Deterministic Python modules hold every fact and rule (receipts, economics, evidence, self-check, PayPal calls); a Deep Agent orchestrates them as tools, with `interrupt_on` on the single write tool and a Postgres checkpointer.

**Tech Stack:** Python 3.12, uv, FastAPI, httpx, psycopg 3 (async pool), cryptography (Ed25519), mcp (FastMCP), deepagents + langchain-openai + langgraph-checkpoint-postgres, reportlab, pytest + anyio.

**Spec:** `docs/design.md` (read it first). Background research and PayPal endpoint gotchas: `../PayPal AI hackathon build plan.md` ("the research plan").

## Global Constraints

- Python 3.12; dependencies via `uv`; run everything as `uv run --env-file .env <cmd>`.
- Not allowed: ORM, Alembic, pgvector, separate worker process, PayPal Agent Toolkit package.
- Receipt commitment: `purchase_units[0].custom_id = "cs1:" + seal_hash` (68 chars).
- Every PayPal POST sends `PayPal-Request-Id`; retries reuse the same ID.
- Dispute actions use `links[].href` from a fresh `GET /v1/customer/disputes/{id}`; never build action URLs by hand.
- No PayPal dispute write and no refund without an unconsumed `approvals` row whose `payload_hash` matches.
- Data returned to any LLM passes through `pii.Redactor` first.
- Money is `Decimal` in code and 2-decimal strings (`"84.00"`) in JSON and PayPal bodies; currency `USD`.
- Timestamps are RFC 3339 UTC strings ending in `Z`.
- Secrets only in env; `.env` is git-ignored; never log tokens or keys.
- Tests that need Postgres use `TEST_DATABASE_URL` and are skipped when it is unset. Tests never call live PayPal or a live LLM, except those marked `llm`.
- Each commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A dispute with no receipt and no due date** (paid outside Countersign, or PayPal omits `seller_response_due_date`) should still produce a case: strength `NONE`, due = create time + 10 days. Test in Task 11.
2. **Confirm clicked twice** should return the same PayPal order, not create a second one. Test in Task 6.
3. **The webhook and the poller both seeing a new dispute** should start exactly one agent run. Test in Task 15.
4. **Approve clicked twice** should submit to PayPal once; the second attempt should be rejected. Test in Task 11.
5. **The LLM provider fails** (429, malformed output, no tool call) and the case should become `NEEDS_ATTENTION` with its triage fields intact, never hang in `ANALYZING`. Test in Task 13.

---

## File map

```
pyproject.toml  .gitignore  .env.example  LICENSE  README.md  render.yaml
app/
  __init__.py
  config.py        Settings + load_settings()
  db.py            pool, schema, query helpers
  schema.sql       all tables (idempotent)
  paypal.py        PayPalClient, PayPalError
  receipts.py      canonical JSON, chain, seal, sign/verify, strength, JWS, AP2 export, receipt DB helpers
  mandates.py      cart validation, mandate create/confirm/capture/track/cancel/status
  economics.py     reason mapping, fees, deadlines, Seller Protection, expected value, decide()
  disputes.py      ingest, case inputs, evidence, playbooks, check_draft, payload hash, submit_approved
  pdf.py           evidence pack PDF with QR
  pii.py           Redactor
  llm.py           chat_model(), json_call()
  agent.py         Deep Agent tools, prompts, fact-checker subagent, run_case(), resume_case()
  issues.py        report_issue proposal + resolution
  auth.py          seller session tokens, MCP agent keys
  mcp_server.py    FastMCP tools
  webhooks.py      PayPal webhook receiver + dispatch
  loops.py         dispute poller, expiry sweeper
  api.py           HTTP routes
  main.py          create_app(), lifespan
corpus/playbooks/  INR.md SNAD.md UNAUTHORIZED.md DUPLICATE.md CREDIT_NOT_PROCESSED.md OTHER.md AGENT_INTENT.md
scripts/           make_keys.py seed.py buyer_agent.py demo_reset.py spikes/s1..s9_*.py
tests/             conftest.py fixtures/ test_*.py
docs/              design.md plan.md verified.md eval-results.md
```

---

### Task 0: Owner prerequisites (owner does these; no code)

- [ ] PayPal sandbox app + 1 US business + 5 US personal buyers (steps in the design doc §18 and the chat handoff).
- [ ] Neon (neon.tech) free project with two databases, `countersign` and `countersign_test`.
- [ ] Gemini API key (aistudio.google.com); Groq key optional; Nebius key later.
- [ ] Create `.env` from `.env.example` (written in Task 1) with: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `DATABASE_URL`, `TEST_DATABASE_URL`, `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_FAST_MODEL`, `ADMIN_PASSWORD`, `SESSION_SECRET` (any 32+ random chars), `PAYPAL_SANDBOX_BUYERS` (JSON list of `{"email","payer_id"}`).

---

### Task 1: Project skeleton, config, health check

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `LICENSE` (MIT, "2026 Bhuvansai"), `app/__init__.py`, `app/config.py`, `app/main.py`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.Settings` frozen dataclass with fields: `paypal_client_id, paypal_client_secret, paypal_base_url="https://api-m.sandbox.paypal.com", paypal_webhook_id="", paypal_sandbox_buyers: list[dict]=[], database_url, llm_base_url="", llm_api_key="", llm_model="", llm_fast_model="", merchant_signing_key, mcp_agent_keys: dict[str,str] (key -> agent_id), admin_password, session_secret, frontend_origin="http://localhost:3000", public_base_url="http://localhost:8000", simulator_enabled=False, allow_simulated_webhooks=False, demo_enabled=False, key_id="k1", merchant_name="Maya's Loom (sandbox)"`.
  - `config.load_settings(env: Mapping[str,str] = os.environ) -> Settings`. Required: `PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, DATABASE_URL, MERCHANT_SIGNING_KEY, ADMIN_PASSWORD, SESSION_SECRET`; a missing one raises `RuntimeError` naming all missing vars. `MCP_AGENT_KEYS` format: `agt_demo:key1,agt_claude:key2`. Booleans: `"true"` (case-insensitive) is true.
  - `main.create_app(settings: Settings, *, pool=None, paypal=None) -> FastAPI` (later tasks add routers and lifespan work); `GET /healthz` → `{"ok": true}` for now.
  - `tests/conftest.py`: fixture `settings` (a valid `Settings` built from a dict with fake values and a fresh `MERCHANT_SIGNING_KEY` from `receipts.new_signing_seed()` once Task 5 exists; until then a fixed base64 of 32 zero bytes), fixture `anyio_backend` returning `"asyncio"`.

- [ ] **Step 1:** `uv init --app --python 3.12 --name countersign-backend`, then `uv add fastapi "uvicorn[standard]" httpx "psycopg[binary]" psycopg-pool cryptography mcp reportlab deepagents langchain-openai langgraph-checkpoint-postgres` and `uv add --dev pytest anyio`. In `pyproject.toml` add `[tool.pytest.ini_options] markers = ["llm: calls a real LLM"]`, `addopts = "-m 'not llm'"`. `.gitignore`: `.env`, `.venv/`, `__pycache__/`, `*.pdf`, `.pytest_cache/`. `.env.example` lists every variable in design §15 plus `TEST_DATABASE_URL`, with empty values.
- [ ] **Step 2: Write failing tests** in `tests/test_config.py`:
  - `test_missing_required_vars_named`: `load_settings({})` raises `RuntimeError` whose message contains `PAYPAL_CLIENT_ID` and `SESSION_SECRET`.
  - `test_agent_keys_parsed`: `MCP_AGENT_KEYS="agt_a:k1,agt_b:k2"` → `settings.mcp_agent_keys == {"k1": "agt_a", "k2": "agt_b"}`.
  - `test_bool_flags`: `SIMULATOR_ENABLED="TRUE"` → `True`; unset → `False`.
  - `test_healthz`: `httpx.AsyncClient(transport=ASGITransport(create_app(settings)))` GET `/healthz` → 200, `{"ok": True}` (mark `@pytest.mark.anyio`).
- [ ] **Step 3:** Run `uv run pytest tests/test_config.py -v` → FAIL (import errors).
- [ ] **Step 4:** Implement `config.py` and `main.py` to the interfaces above.
- [ ] **Step 5:** Run `uv run pytest -v` → all PASS.
- [ ] **Step 6: Commit** `chore: project skeleton, config and health check`.

---

### Task 2: Database schema and helpers

**Files:**
- Create: `app/schema.sql`, `app/db.py`, `tests/test_db.py`
- Modify: `tests/conftest.py` (add `db` fixture)

**Interfaces:**
- Produces:
  - `db.open_pool(url: str) -> psycopg_pool.AsyncConnectionPool` (opened, `kwargs={"row_factory": dict_row, "autocommit": True}`).
  - `db.apply_schema(pool) -> None` (runs `schema.sql`; safe to run repeatedly).
  - `db.fetch(pool, sql, *args) -> list[dict]`, `db.fetchrow(pool, sql, *args) -> dict | None`, `db.execute(pool, sql, *args) -> None`. Placeholders are `%s`.
  - conftest fixture `db`: skips if `TEST_DATABASE_URL` unset; opens a pool, applies the schema, `TRUNCATE` all tables `RESTART IDENTITY CASCADE` before each test; yields the pool.
- `schema.sql` (exact; later tasks rely on these names):

```sql
CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  price NUMERIC(12,2) NOT NULL, cost NUMERIC(12,2) NOT NULL DEFAULT 0,
  attributes JSONB NOT NULL DEFAULT '{}', image_url TEXT);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, claimed_platform TEXT NOT NULL, public_key TEXT,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('PENDING_CONFIRMATION','CONFIRMED','ORDERED','CAPTURED','CANCELLED','EXPIRED')),
  instruction TEXT NOT NULL, constraints JSONB NOT NULL, cart JSONB NOT NULL,
  agent_id TEXT REFERENCES agents(id), agent_sig TEXT, agent_sig_ok BOOLEAN,
  confirmation_method TEXT, seal_hash CHAR(64), merchant_sig TEXT, key_id TEXT NOT NULL DEFAULT 'k1',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS receipt_events (
  receipt_id TEXT NOT NULL REFERENCES receipts(id), seq INT NOT NULL, type TEXT NOT NULL,
  payload JSONB NOT NULL, at TEXT NOT NULL, prev_hash CHAR(64) NOT NULL, hash CHAR(64) NOT NULL,
  PRIMARY KEY (receipt_id, seq));
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'receipt_events is append-only'; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS receipt_events_immutable ON receipt_events;
CREATE TRIGGER receipt_events_immutable BEFORE UPDATE OR DELETE ON receipt_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TABLE IF NOT EXISTS orders (
  paypal_order_id TEXT PRIMARY KEY, receipt_id TEXT REFERENCES receipts(id), capture_id TEXT UNIQUE,
  status TEXT NOT NULL, amount NUMERIC(12,2) NOT NULL, currency CHAR(3) NOT NULL DEFAULT 'USD',
  payer_id TEXT, shipping_address JSONB, custom_id TEXT, request_id UUID NOT NULL, raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS shipments (
  id BIGSERIAL PRIMARY KEY, paypal_order_id TEXT NOT NULL REFERENCES orders(paypal_order_id),
  tracker_id TEXT, carrier TEXT NOT NULL, tracking_number TEXT NOT NULL, status TEXT NOT NULL,
  delivered_at TIMESTAMPTZ, raw JSONB);
CREATE TABLE IF NOT EXISTS disputes (
  dispute_id TEXT PRIMARY KEY, capture_id TEXT, paypal_order_id TEXT, reason TEXT, status TEXT, stage TEXT,
  amount NUMERIC(12,2), currency CHAR(3), created_at_pp TIMESTAMPTZ, due_at TIMESTAMPTZ,
  raw JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cases (
  dispute_id TEXT PRIMARY KEY REFERENCES disputes(dispute_id),
  state TEXT NOT NULL CHECK (state IN ('NEW','ANALYZING','AWAITING_APPROVAL','SUBMITTED','WON','LOST','ACCEPTED','EXPIRED','NEEDS_ATTENTION','MANUAL')),
  thread_id TEXT, triage JSONB, economics JSONB, override JSONB, receipt_strength TEXT,
  evidence JSONB, evidence_gaps JSONB, draft JSONB, check_issues JSONB,
  pdf BYTEA, pdf_sha256 CHAR(64), payload JSONB, payload_hash CHAR(64), pending_action JSONB,
  error TEXT, outcome TEXT, dry_run BOOLEAN NOT NULL DEFAULT false, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS issues (
  id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL REFERENCES receipts(id), capture_id TEXT,
  type TEXT NOT NULL, description TEXT NOT NULL, desired_resolution TEXT NOT NULL,
  proposal JSONB, state TEXT NOT NULL CHECK (state IN ('OPEN','PROPOSED','OFFERED','RESOLVED','REJECTED')),
  countered BOOLEAN NOT NULL DEFAULT false, resolution JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS approvals (
  id BIGSERIAL PRIMARY KEY, dispute_id TEXT REFERENCES disputes(dispute_id), issue_id TEXT REFERENCES issues(id),
  user_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('PROVIDE_EVIDENCE','MAKE_OFFER','ACCEPT_CLAIM','REFUND','REPLACEMENT')),
  payload_hash CHAR(64) NOT NULL, approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  consumed_at TIMESTAMPTZ, response JSONB, http_status INT);
CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, verified BOOLEAN NOT NULL, resource_id TEXT,
  payload JSONB NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS history_txns (
  id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL, amount NUMERIC(12,2) NOT NULL,
  disputed BOOLEAN NOT NULL DEFAULT false, simulated BOOLEAN NOT NULL DEFAULT true);
CREATE INDEX IF NOT EXISTS disputes_due ON disputes (due_at);
```

- [ ] **Step 1: Write failing tests** in `tests/test_db.py` (all `@pytest.mark.anyio`, use `db`):
  - `test_schema_idempotent`: `apply_schema` twice raises nothing.
  - `test_receipt_events_append_only`: insert a `receipts` row and one `receipt_events` row; `UPDATE receipt_events SET type='X'` raises `psycopg.errors.RaiseException` with "append-only"; `DELETE` raises the same.
- [ ] **Step 2:** Run `uv run --env-file .env pytest tests/test_db.py -v` → FAIL.
- [ ] **Step 3:** Implement `schema.sql` (above) and `db.py`.
- [ ] **Step 4:** Run again → PASS. Wire `create_app` lifespan: when `pool` is not injected, `open_pool(settings.database_url)` + `apply_schema`; `/healthz` adds `"db": true/false` from `SELECT 1`.
- [ ] **Step 5: Commit** `feat: postgres schema with append-only receipt events`.

---

### Task 3: PayPal REST client

**Files:**
- Create: `app/paypal.py`, `tests/test_paypal.py`
- Modify: `tests/conftest.py` (add `FakePayPal`)

**Interfaces:**
- Produces:
  - `class PayPalError(Exception)` with attributes `status: int`, `name: str | None` (PayPal error `name`), `body: dict`.
  - `class PayPalClient(base_url: str, client_id: str, secret: str, transport: httpx.AsyncBaseTransport | None = None)`:
    - `async token() -> str` (cached; refreshed when `expires_in - 300` s have passed; guarded by an `asyncio.Lock`), `token_ttl() -> int` (seconds left, 0 if none).
    - `async request(method: str, path_or_url: str, *, json: Any = None, files: dict | None = None, data: dict | None = None, request_id: str | None = None, mock: str | None = None, prefer_representation: bool = False) -> dict`. Absolute URLs (HATEOAS hrefs) are used as-is. POST without `request_id` gets a new uuid4. On 401: refresh token once and retry with the same `request_id`. Non-2xx → `PayPalError`. `mock` sets `PayPal-Mock-Response: {"mock_application_codes": "<mock>"}`. Empty body → `{}`.
    - Thin wrappers: `create_order(body, request_id)`, `get_order(order_id)`, `capture_order(order_id, request_id, mock=None)`, `track_order(order_id, body, request_id)`, `update_tracker(order_id, tracker_id, patch: list[dict])` (PATCH `/v2/checkout/orders/{id}/trackers/{tracker_id}`), `get_capture(capture_id)`, `refund_capture(capture_id, amount: str, request_id, note: str)`, `list_disputes(params: dict)`, `get_dispute(dispute_id)`, `post_link(href, *, json=None, files=None, data=None, request_id)`, `verify_webhook(headers: Mapping[str,str], event: dict, webhook_id: str) -> bool` (`verification_status == "SUCCESS"`). Order calls use `prefer_representation=True`.
  - conftest `FakePayPal`: builds a `PayPalClient` over `httpx.MockTransport`; `routes: dict[tuple[str, str], dict | Callable[[httpx.Request], httpx.Response]]` keyed by `(METHOD, path_prefix)`; always answers `/v1/oauth2/token` with `{"access_token":"T","expires_in":32400}`; records every request in `.calls: list[httpx.Request]`.

- [ ] **Step 1: Write failing tests** in `tests/test_paypal.py` (`@pytest.mark.anyio`):
  - `test_token_cached`: two `get_order` calls → exactly one request to `/v1/oauth2/token`.
  - `test_401_refreshes_once_same_request_id`: first `POST /v2/checkout/orders` returns 401, second 201 → two token calls, two order calls, both with identical `PayPal-Request-Id`.
  - `test_post_has_request_id`: `capture_order("O1", request_id="rid-1")` → header `PayPal-Request-Id == "rid-1"`.
  - `test_mock_header`: `capture_order("O1", "r", mock="INSTRUMENT_DECLINED")` → header `PayPal-Mock-Response == '{"mock_application_codes": "INSTRUMENT_DECLINED"}'`.
  - `test_error_raises`: route returns 403 `{"name":"PERMISSION_DENIED"}` for `GET /v1/customer/disputes/ERRDIS015` → `PayPalError` with `status == 403`, `name == "PERMISSION_DENIED"`.
  - `test_post_link_uses_absolute_href`: `post_link("https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/make-offer", json={...}, request_id="r")` → request URL equals that href.
- [ ] **Step 2:** Run `uv run pytest tests/test_paypal.py -v` → FAIL.
- [ ] **Step 3:** Implement `paypal.py`; token via Basic auth `POST /v1/oauth2/token` with `grant_type=client_credentials`.
- [ ] **Step 4:** Run → PASS. `/healthz` adds `"paypal_token_ttl"`.
- [ ] **Step 5: Commit** `feat: paypal client with token cache, request ids and mock header`.

---

### Task 4: PayPal sandbox spikes S1–S5, S7, S9 (verification, not TDD)

**Files:**
- Create: `scripts/spikes/common.py` (loads settings, builds `PayPalClient`), `scripts/spikes/s1_order_flow.py`, `s2_card_order.py`, `s3_create_dispute.py`, `s4_provide_evidence.py`, `s5_adjudicate.py`, `s7_offer_accept.py`, `s9_paypal_mcp.py`, `docs/verified.md`

Each script prints full request and response JSON (tokens redacted) and appends a section to `docs/verified.md`: what was sent, what came back, PASS/FAIL, and the fallback chosen. Owner involvement is marked **(owner)**.

- [ ] **S1** `s1_order_flow.py`: create order with `custom_id = "cs1:" + "a"*64`, print the `payer-action` (or `approve`) link; **(owner)** opens it and approves as a sandbox buyer; script waits for Enter, captures, tracks (`carrier: "USPS"`, `tracking_number: "9400100000000000000000"`, `notify_payer: false`, `capture_id`, `items` from the order), then GETs the order. Record: `custom_id` max length accepted, capture ID path, tracker ID, whether `seller_protection` appears in the capture, the buyer's `payer_id`.
- [ ] **S2** `s2_card_order.py`: order with `payment_source.card` (sandbox test card from the Dashboard card generator), capture without buyer login. Record pass/fail.
- [ ] **S3** `s3_create_dispute.py --mode assertion|chargeback`. `assertion`: `POST /v1/customer/disputes` multipart `input` JSON (`disputed_transactions:[{"buyer_transaction_id": ...}]`, `reason: "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"`, `dispute_amount`, `extensions`, `evidences:[{"notes":"ACCELERATED_RESPONSE_ENABLED"}]`), header `PayPal-Auth-Assertion` = `b64url({"alg":"none"}) + "." + b64url({"iss": client_id, "email": buyer_email}) + "."`. Try the capture ID and then the buyer-side transaction ID (owner reads it from the buyer's sandbox activity page) as `buyer_transaction_id`. `chargeback`: `POST /v2/customer-support/process-chargeback` on an S2 card capture with the fields from research plan §7 row 21. If both fail, **(owner)** opens a dispute in the sandbox Resolution Center as the buyer (fallback c). Record which path works; this decides Task 16's simulator and the judge demo.
- [ ] **S4** `s4_provide_evidence.py <dispute_id>`: poll `get_dispute` every 30 s (max 15 min) until `links[]` has `provide_evidence`; record all `rel` names and `allowed_response_options`; POST multipart: part `input` (`application/json`) = `{"evidences":[{"evidence_type":"PROOF_OF_FULFILLMENT","evidence_info":{"tracking_info":[{"carrier_name":"USPS","tracking_number":"..."}]},"notes":"Spike","documents":[{"name":"evidence.pdf"}]}]}`, part `evidence-file` = a one-page PDF. Record the status code and the dispute status afterwards.
- [ ] **S5** `s5_adjudicate.py <dispute_id>`: after S4, poll for `status == "UNDER_REVIEW"` and an `adjudicate` link; try `escalate` if offered; then `adjudicate {"adjudication_outcome":"SELLER_FAVOR"}`. Record the path to `UNDER_REVIEW` and the `dispute_outcome` field name and values on the resolved dispute.
- [ ] **S7** `s7_offer_accept.py <dispute_id> offer|accept`: `make-offer` `{"note":"Spike offer","offer_type":"REFUND","offer_amount":{"currency_code":"USD","value":"10.00"}}`; `accept-claim` multipart `input` `{"note":"Spike accept"}`. Record status codes.
- [ ] **S9 (optional)** `s9_paypal_mcp.py`: call remote `https://mcp.sandbox.paypal.com/http` tool `get_dispute` with `Authorization: Bearer <access token>`. Record pass/fail.
- [ ] **Finish:** `docs/verified.md` lists, for each spike, PASS/FAIL and the decision for: dispute creation path, `links[].rel` names, provide-evidence `input` schema, path to `UNDER_REVIEW`, outcome field and values. Commit `docs: sandbox verification results` (with scripts).

---

### Task 5: Receipts (pure crypto + DB helpers) and key script

**Files:**
- Create: `app/receipts.py`, `scripts/make_keys.py`, `tests/test_receipts.py`

**Interfaces:**
- Produces:
  - `canonical(x) -> bytes` = `json.dumps(x, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()`.
  - `now_z() -> str` (UTC, seconds precision, `Z`).
  - `GENESIS = "0"*64`; `PRE_SEAL = {"MANDATE_CREATED","CART_SNAPSHOT","BUYER_CONFIRMED","AGENT_RELAYED_CONFIRMATION"}`.
  - `event_hash(prev_hash: str, seq: int, type: str, at: str, payload: dict) -> str` = `sha256(prev_hash.encode() + canonical({"seq","type","at","payload"})).hexdigest()`.
  - `verify_chain(events: list[dict]) -> bool` (events have `seq,type,at,payload,prev_hash,hash`; seq contiguous from 0; links and hashes recompute).
  - `seal_hash_of(events) -> str | None` (hash of the last `PRE_SEAL` event).
  - `seal_payload(receipt_id, seal_hash, merchant_name, created_at) -> bytes` = `canonical({"v":"cs1","receipt_id","seal_hash","merchant":merchant_name,"created_at"})`.
  - `new_signing_seed() -> str` (base64 of 32 random bytes); `load_key(seed_b64) -> Ed25519PrivateKey`; `public_key_b64(key) -> str` (raw 32 bytes, base64).
  - `sign(key, data: bytes) -> str` (base64), `verify_sig(public_key_b64: str, data: bytes, sig_b64: str) -> bool` (False on any error).
  - `custom_id_for(seal_hash) -> str` = `"cs1:" + seal_hash`.
  - `strength(events, sig_ok: bool, custom_id_match: bool) -> str`: `STRONG` if a `BUYER_CONFIRMED` event has `payload.method == "hosted_page_click"` and a `PAYPAL_APPROVED` event exists and `sig_ok and custom_id_match`; `MEDIUM` if `PAYPAL_APPROVED` and `AGENT_RELAYED_CONFIRMATION` exist and `sig_ok`; else `WEAK`.
  - `verify_receipt(receipt: dict, events: list[dict], paypal_custom_id: str | None, public_key: str) -> dict` → `{"chain_ok","seal_ok","sig_ok","custom_id_match","strength"}`. `seal_ok`: `receipt["seal_hash"] == seal_hash_of(events)`.
  - `jws_eddsa(key, payload: dict, kid: str) -> str`: compact JWS. `header = {"alg":"EdDSA","typ":"JWT","kid":kid}`; signing input = `b64url(canonical(header)) + "." + b64url(canonical(payload))` (base64url, no padding); signature = Ed25519 over the ASCII signing input.
  - DB helpers: `async append_event(pool, receipt_id: str, type: str, payload: dict) -> dict` (one transaction: `SELECT ... FROM receipts WHERE id=%s FOR UPDATE`, read last event, insert seq+1 with chained hash, `at = now_z()`; returns the event), `async load_events(pool, receipt_id) -> list[dict]` (ordered by seq), `async load_receipt(pool, receipt_id) -> dict | None`.
- `scripts/make_keys.py` prints `MERCHANT_SIGNING_KEY=<new_signing_seed()>`.

- [ ] **Step 1: Write failing tests** in `tests/test_receipts.py`. Helper `_chain(types)` builds events with `event_hash`.
  - `test_canonical_sorted_compact`: `canonical({"b":1,"a":"é"}) == '{"a":"é","b":1}'.encode()`.
  - `test_chain_verifies` / `test_tamper_payload_fails` (change one payload value → `verify_chain` False) / `test_tamper_reorder_fails` (swap two events → False).
  - `test_seal_is_last_pre_seal_event`: chain `MANDATE_CREATED, CART_SNAPSHOT, BUYER_CONFIRMED, ORDER_CREATED` → `seal_hash_of` equals the hash of index 2.
  - `test_sign_verify_and_bad_sig`: sign/verify round-trips; flipping one byte of the data → False; wrong public key → False.
  - `test_custom_id_len`: `len(custom_id_for("f"*64)) == 68`.
  - `test_strength_cases`: hosted + approved + sig + match → `STRONG`; same with `custom_id_match=False` → `WEAK`; relayed + approved + sig → `MEDIUM`; no `PAYPAL_APPROVED` → `WEAK`.
  - `test_verify_receipt_custom_id_mismatch`: `paypal_custom_id="cs1:"+"0"*64` → `custom_id_match False`, `strength WEAK`.
  - `test_jws_verifies`: split the JWS; `verify_sig(pub, signing_input_bytes, b64(raw_sig))` is True; header decodes to `alg == "EdDSA"`.
  - DB (`db` fixture): `test_append_event_chains`: three `append_event` calls produce seq 0,1,2 and `verify_chain(load_events(...))` is True.
- [ ] **Step 2:** Run `uv run --env-file .env pytest tests/test_receipts.py -v` → FAIL.
- [ ] **Step 3:** Implement `receipts.py` and `make_keys.py`. Update the conftest `settings` fixture to use `new_signing_seed()`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: hash-chained, ed25519-signed intent receipts`.

---

### Task 6: Mandates and checkout

**Files:**
- Create: `app/mandates.py`, `tests/test_mandates.py`

**Interfaces:**
- Consumes: Task 3 `PayPalClient`, Task 5 receipt functions, Task 2 db helpers.
- Produces:
  - `SHIPPING = Decimal("6.00")`; `MANDATE_TTL = timedelta(hours=24)`.
  - `class MandateError(Exception)` with `status: int` and `violations: list[dict]`.
  - `validate_cart(products: dict[str, dict], items: list[dict], max_amount: Decimal, attributes: dict) -> tuple[dict, list[dict]]`: quote = `{"items":[{"sku","name","qty","unit_price"}],"shipping","total"}` (strings); violations are dicts with `code` in `UNKNOWN_SKU`, `BAD_QTY` (qty < 1), `OVER_CAP` (total > max_amount), `ATTRIBUTE_MISMATCH` (any `attributes[k]` differs from the product's `attributes[k]`; include `sku, attribute, wanted, got`).
  - `async create_mandate(pool, settings, *, instruction: str, max_amount: str, currency: str, attributes: dict, items: list[dict], agent_id: str, agent_platform: str, agent_public_key: str | None = None, agent_sig: str | None = None) -> dict` → `{"mandate_id","quote","confirm_url","expires_at","agent_signed": bool}`. Raises `MandateError(422, violations)` if any violation. Id: `"rcpt_" + secrets.token_hex(12)`. Upserts `agents` (pins `public_key` on first use; a different key later → `MandateError(409)`). Agent signature verified over `canonical({"instruction","max_amount","currency","attributes","items"})`. Appends `MANDATE_CREATED` `{instruction, constraints:{max_amount,currency,attributes,expires_at}, agent:{id, claimed_platform, signed}}` and `CART_SNAPSHOT` `{cart: quote}`. `confirm_url = f"{frontend_origin}/confirm/{id}"`.
  - `async get_mandate(pool, mandate_id) -> dict` (public summary: instruction, constraints, cart, agent, status, paypal_order_id, approve_url).
  - `async confirm_mandate(pool, pp, settings, mandate_id, method: str, user_agent: str) -> dict` → `{"paypal_order_id","approve_url"}`. `method` in `{"hosted_page_click","agent_relayed"}`. Expired → `MandateError(410)`; cancelled → 409. **If already `ORDERED`, return the existing order without calling PayPal.** Otherwise: append `BUYER_CONFIRMED {method, ua_hash: sha256(user_agent)}` (or `AGENT_RELAYED_CONFIRMATION {}`); seal = `seal_hash_of`; `merchant_sig = sign(seal_payload(...))`; store `seal_hash, merchant_sig, confirmation_method`, status `CONFIRMED`; create the order (body in design and research plan §7 row 3: `intent: CAPTURE`, `purchase_units[0]` with `reference_id`, `custom_id`, `invoice_id = mandate_id`, `amount` with `breakdown`, `items[]` with `category: "PHYSICAL_GOODS"`; `payment_source.paypal.experience_context` with `return_url = f"{public_base_url}/dev/return"`, `cancel_url`, `user_action: "PAY_NOW"`, `shipping_preference: "GET_FROM_FILE"`, `brand_name`); insert `orders` row with the `request_id`; append `ORDER_CREATED {paypal_order_id, custom_id}`; status `ORDERED`. `approve_url` = link with `rel` `payer-action` or `approve`.
  - `async capture(pool, pp, order_id: str) -> dict` → `{"status","capture_id"}`; only for orders in the `orders` table (else `MandateError(404)`); idempotent if `capture_id` already stored; stores `capture_id`, `payer_id`, `shipping_address`, status; appends `PAYPAL_APPROVED {payer_id_hash}` and `CAPTURED {capture_id, amount}`; receipt status `CAPTURED`.
  - `async add_tracking(pool, pp, order_id, carrier, tracking_number, status="SHIPPED", notify_payer=False) -> dict` → `{"tracker_id"}`; inserts `shipments`; appends `TRACKING_ADDED`.
  - `async update_tracking(pool, pp, order_id, status: str) -> dict`: PATCH the latest tracker with `[{"op":"replace","path":"/status","value":status}]` (confirm the body in spike S1); updates `shipments.status`, sets `delivered_at` when `DELIVERED`; appends `TRACKING_UPDATED {tracker_id, status}`.
  - `async cancel_mandate(pool, mandate_id) -> dict`, `async mandate_status(pool, mandate_id) -> dict` (`status`, `paypal_order_id`, `approve_url`, `capture_id`).

- [ ] **Step 1: Write failing tests** in `tests/test_mandates.py`. Pure: `test_over_cap` (one $78 item + $6 shipping, cap 80 → `OVER_CAP`), `test_attribute_mismatch` (wanted `color=sage`, product `color=blue` → `ATTRIBUTE_MISMATCH`), `test_unknown_sku`, `test_quote_total` (78 + 6 → `"84.00"`). DB + `FakePayPal` (order create returns `{"id":"O1","links":[{"rel":"payer-action","href":"https://x/approve"}]}`):
  - `test_create_mandate_422_on_violation`.
  - `test_confirm_commits_custom_id`: the order-create request body has `purchase_units[0].custom_id == "cs1:" + receipt.seal_hash`, and `seal_hash` equals the hash of the `BUYER_CONFIRMED` event.
  - `test_confirm_twice_one_order` (Review Focus 2): two `confirm_mandate` calls → one `POST /v2/checkout/orders`, same `paypal_order_id`.
  - `test_expired_mandate_410`.
  - `test_capture_appends_events_and_strength_strong`: after confirm + capture (fake capture JSON with `purchase_units[0].payments.captures[0].id = "C1"`), `verify_receipt(..., paypal_custom_id=<sent custom_id>)` returns `STRONG`.
  - `test_agent_key_pinned`: second mandate from the same agent ID with a different public key → `MandateError` 409.
  - `test_update_tracking_delivered`: after `add_tracking`, `update_tracking(..., "DELIVERED")` sends one PATCH, sets `delivered_at`, and appends `TRACKING_UPDATED`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: mandates, confirm with custom_id commitment, capture and tracking`.

---

### Task 7: Seller auth and checkout/receipt HTTP routes

**Files:**
- Create: `app/auth.py`, `app/api.py`, `tests/test_api_checkout.py`
- Modify: `app/main.py` (include router, CORS for `frontend_origin`, store `pool`, `paypal`, `settings`, signing key on `app.state`)

**Interfaces:**
- Produces:
  - In `app/main.py`: `@dataclass class AppState: settings: Settings; pool: AsyncConnectionPool; pp: PayPalClient; key: Ed25519PrivateKey; checkpointer: Any = None`, stored at `app.state.cs`. Every later task's `state` parameter is this object; route handlers get it via `request.app.state.cs`.
  - `auth.make_token(secret: str, ttl_s: int = 43200) -> str` (`b64url(json{"exp"}) + "." + hmac_sha256_hex`), `auth.check_token(secret, token) -> bool`, FastAPI dependency `auth.require_seller(request) -> str` (returns `"seller"`, else 401), `auth.agent_for_key(settings, authorization_header) -> str | None`.
  - Routes (design §12): `POST /api/login {password}` (`hmac.compare_digest`; 401 on mismatch) → `{token}`; `GET /api/products`; `POST /api/mandates` (seller-only; same fields as `create_mandate`); `GET /api/mandates/{id}` (public); `POST /api/mandates/{id}/confirm {method}` (public, passes `User-Agent`); `POST /api/orders/{id}/capture` (public); `POST /api/orders/{id}/track` (seller); `GET /api/orders` (seller; rows with receipt strength); `GET /api/receipts/{id}` (seller); `GET /api/receipts/{id}/verify` (public; calls `pp.get_order` for the live `custom_id`); `GET /.well-known/countersign-keys.json` → `{"keys":[{"kid","ed25519_pub"}]}`; `GET /verify/{receipt_id}` (public HTML: strength badge, chain/sig/custom_id checks, receipt hashes); `GET /dev/return?token=` (only if `simulator_enabled`; calls `capture` for `token` = order ID, renders result).
  - `MandateError` maps to its `status` with `{"detail", "violations"}`. `PayPalError` maps to PayPal's status if it is 4xx (else 502) with `{"detail": error.name, "paypal": error.body}`.
  - `POST /api/orders/{id}/track` also accepts `{"status": "DELIVERED"}` alone, which calls `update_tracking`.

- [ ] **Step 1: Write failing tests** (`httpx.AsyncClient` + `ASGITransport`, injected `db` pool and `FakePayPal`): `test_capture_declined_maps_error` (fake capture returns 422 `{"name":"UNPROCESSABLE_ENTITY","details":[{"issue":"INSTRUMENT_DECLINED"}]}` → response 422 whose `paypal.details[0].issue == "INSTRUMENT_DECLINED"`), `test_login_ok_and_bad`, `test_seller_route_needs_token` (GET `/api/orders` without token → 401), `test_mandate_confirm_capture_verify_flow` (create via `/api/mandates` → confirm → capture → `/verify` JSON has `strength == "STRONG"`), `test_keys_endpoint_matches_signing_key`, `test_dev_return_404_when_simulator_off`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: seller auth and checkout, receipt and verify routes`.

---

### Task 8: PII redaction and LLM client (+ spike S8 part 1)

**Files:**
- Create: `app/pii.py`, `app/llm.py`, `scripts/spikes/s8_llm.py`, `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `class Redactor`: `redact(obj) -> obj` walks dicts/lists/strings. It replaces values under keys `email, email_address, name, given_name, surname, full_name, phone, phone_number, address_line_1, address_line_2, admin_area_1, admin_area_2, postal_code` and any email or phone pattern inside strings (`[\w.+-]+@[\w-]+\.[\w.]+`, `\+?\d[\d\s().-]{8,}\d`) with stable tokens like `<EMAIL_1>`, `<NAME_1>`, `<PHONE_1>`, `<ADDRESS_1>`. The same value always maps to the same token. `restore(text: str) -> str` reverses it.
  - `llm.chat_model(settings, fast: bool = False) -> ChatOpenAI` (`model = llm_fast_model if fast else llm_model`, `base_url`, `api_key`, `temperature=0`, `max_retries=2`).
  - `async llm.json_call(model, system: str, user: str, schema: type[BaseModel]) -> BaseModel`: invoke with `response_format={"type":"json_object"}` bound; parse with `schema.model_validate_json`; on failure retry once with the validation error appended; then raise `LLMError`.
- `scripts/spikes/s8_llm.py`: for the configured provider, `GET {LLM_BASE_URL}models` (print the IDs), then one `json_call` returning `{"ok": true}`. Run it for Gemini (and Groq if keyed); record in `docs/verified.md`.

- [ ] **Step 1: Write failing tests**: `test_redact_nested` (dispute JSON with `buyer.email`, `shipping.address.address_line_1`, a message containing `call me +1 415 555 0100` → no original value appears in `json.dumps(redacted)`); `test_redact_stable_and_restore` (same email twice → same token; `restore` brings it back); `test_json_call_retries_once` (fake model returning `"not json"` then `'{"ok": true}'` → parsed on the second call); `test_json_call_raises_after_two_failures`. Fake model: a `langchain_core` `GenericFakeChatModel` subclass whose `bind` returns `self`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement; `/healthz` adds `"llm_model"`. **Step 4:** Run → PASS. **Step 5:** Run `uv run --env-file .env python scripts/spikes/s8_llm.py`; record the model IDs and pass/fail.
- [ ] **Step 6: Commit** `feat: pii redaction and openai-compatible llm client`.

---

### Task 9: MCP server and buyer agent script

**Files:**
- Create: `app/mcp_server.py`, `scripts/buyer_agent.py`, `tests/test_mcp.py`
- Modify: `app/main.py` (mount at `/mcp`, run the MCP session manager in the lifespan, wrap the mount in an ASGI check that `auth.agent_for_key` accepts the `Authorization` header, else 401)

**Interfaces:**
- Consumes: Task 6 mandate functions, Task 5 `verify_receipt`.
- Produces: `build_mcp(state) -> FastMCP` (`FastMCP("countersign", stateless_http=True)`; `state` holds pool, pp, settings). Tools (the agent ID comes from the API key, never from arguments):
  - `search_products(query: str = "", max_price: float | None = None) -> list[dict]` (case-insensitive match on name/description/attributes).
  - `create_mandate(instruction: str, max_amount: str, items: list[dict], attributes: dict | None = None, currency: str = "USD", agent_platform: str = "custom", agent_public_key: str | None = None, agent_sig: str | None = None) -> dict` (violations returned as `{"error":"VIOLATIONS","violations":[...]}`, not raised).
  - `get_mandate_status(mandate_id: str) -> dict`, `cancel_mandate(mandate_id: str) -> dict`, `get_receipt(mandate_id: str) -> dict` (receipt + events + `verify_receipt` result).
  - (Task 17 adds `report_issue`, `respond_to_proposal` and `get_issue_status`.)
  - Resource `countersign://policy`: plain-text agent policy ("agents may quote, never pay; a human confirms every purchase; receipts are signed and committed to PayPal").
- `scripts/buyer_agent.py "<instruction>"`: connects to `{PUBLIC_BASE_URL}/mcp` with the `mcp` client (`streamablehttp_client`, header `Authorization: Bearer <MCP key>`), lists the tools, and runs a short tool-calling loop with `chat_model(settings)` (max 6 steps) that searches, creates a mandate and prints the `confirm_url`.

- [ ] **Step 1: Write failing tests** (call tools through `mcp.call_tool(name, args)` on the FastMCP instance with injected state; `db` + `FakePayPal`): `test_tools_listed` (exactly the five names above), `test_create_mandate_tool_returns_confirm_url`, `test_create_mandate_tool_violations_not_raised`, `test_mcp_mount_requires_key` (HTTP POST `/mcp` without `Authorization` → 401).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (check the current `mcp` SDK docs for `streamable_http_app()` and `session_manager.run()` before mounting). **Step 4:** Run → PASS.
- [ ] **Step 5 (manual):** `uv run --env-file .env uvicorn app.main:app --port 8000`, then `npx @modelcontextprotocol/inspector` against `http://localhost:8000/mcp` with the key; call `create_mandate`. Then `uv run --env-file .env python scripts/buyer_agent.py "Buy one linen throw in sage under $90"` prints a confirm URL.
- [ ] **Step 6: Commit** `feat: countersign mcp server and buyer agent script`.

---

### Task 10: Economics (deterministic decision engine + golden decisions)

**Files:**
- Create: `app/economics.py`, `tests/test_economics.py`

**Interfaces:**
- Produces:
  - `normalize_reason(paypal_reason: str) -> str`: `MERCHANDISE_OR_SERVICE_NOT_RECEIVED→INR`, `MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED→SNAD`, `UNAUTHORISED`/`UNAUTHORIZED→UNAUTHORIZED`, `DUPLICATE_TRANSACTION→DUPLICATE`, `CREDIT_NOT_PROCESSED→CREDIT_NOT_PROCESSED`, anything else `→OTHER`.
  - `@dataclass CaseInputs`: `reason: str` (normalized), `amount: Decimal`, `created_at: datetime`, `due_date: datetime | None`, `now: datetime`, `tracking_delivered: bool`, `has_shipping_address: bool`, `capture_sp_status: str | None`, `receipt_strength: str`, `receipt_violation: bool`, `constraints_match_cart: bool`, `refund_covers_amount: bool`, `distinct_duplicate: bool`, `disputes_90d: int`, `txns_90d: int`.
  - `@dataclass Priors` (defaults): `base = {"INR":.35,"SNAD":.25,"UNAUTHORIZED":.30,"DUPLICATE":.20,"CREDIT_NOT_PROCESSED":.20,"OTHER":.20}`, `bonus_sp_inr=.45`, `bonus_strong=.20`, `bonus_delivered=.10`, `bonus_constraints_snad=.25`, `bonus_strong_unauth=.30`, `bonus_refund=.75`, `bonus_duplicate=.60`, `labor=Decimal("5")`, `offer_ratio=Decimal("0.4")`, `p_offer_accept=Decimal("0.5")`.
  - `decide(ci: CaseInputs, priors: Priors = Priors()) -> dict` with keys `decision` (`FIGHT|OFFER|ACCEPT|EXPIRED`), `p_win`, `fee`, `sp_eligible`, `high_volume`, `hv_warning`, `dispute_rate_90d`, `due_at`, `hours_left`, `ev: {fight, offer, accept}`, `offer: {offer_type, amount}`, `evidence_type`, `assumptions` (the priors used), `rules_fired: list[str]`. Decimal values serialized as strings.
- Rules (exact):
  - `rate = disputes_90d / txns_90d` (0 if no txns); `high_volume = rate >= 0.015 and txns_90d > 100`; `hv_warning = rate >= 0.010`.
  - `due_at = due_date or created_at + (3 days if high_volume else 10 days)`; `hours_left = (due_at - now) / 1h`.
  - `sp_eligible = reason == "INR" and tracking_delivered and has_shipping_address and capture_sp_status in (None, "ELIGIBLE", "PARTIALLY_ELIGIBLE")`.
  - `fee = 0 if amount < 30 or sp_eligible else (30 if high_volume else 15)`.
  - `p = base[reason]`, then add: `bonus_sp_inr` if sp_eligible; `bonus_strong` if strength STRONG; `bonus_delivered` if tracking_delivered and not sp_eligible; `bonus_constraints_snad` if SNAD and constraints_match_cart and strength STRONG; `bonus_strong_unauth` if UNAUTHORIZED and STRONG; `bonus_refund` if CREDIT_NOT_PROCESSED and refund_covers_amount; `bonus_duplicate` if DUPLICATE and distinct_duplicate. Clamp to [0.05, 0.95].
  - `fight = (1-p)*(A+F) + L`; `accept = A + F`; `x = round(offer_ratio*A, 2)`; `offer = p_oa*x + (1-p_oa)*fight + L`.
  - Hard rules first: `hours_left < 0 → EXPIRED`; `receipt_violation → ACCEPT`. Otherwise argmin of (fight, offer, accept); ties prefer FIGHT, then OFFER.
  - `offer_type = "REFUND_WITH_RETURN" if reason == "SNAD" else "REFUND"`.
  - `evidence_type = "PROOF_OF_REFUND" if reason == "CREDIT_NOT_PROCESSED" else ("PROOF_OF_FULFILLMENT" if reason == "INR" else "OTHER")`.

- [ ] **Step 1: Write failing tests**. Helper `ci(**overrides)` with defaults: `amount=Decimal("84.00")`, `created_at=now-1d`, `due_date=None`, all bools False, strength `NONE`, `disputes_90d=1, txns_90d=120`, `capture_sp_status=None`. Golden cases (plan G-numbers):
  - `G1`: INR, $80, delivered, address → `FIGHT`, `fee == "0"`, `sp_eligible`.
  - `G2`: INR, $45, nothing → `OFFER`.
  - `G3`: SNAD, STRONG, constraints match, delivered → `FIGHT`.
  - `G4`: SNAD, $60, delivered, strength NONE → `OFFER` with `offer_type == "REFUND_WITH_RETURN"`.
  - `G5`: UNAUTHORIZED, STRONG → `FIGHT`.
  - `G6`: UNAUTHORIZED, MEDIUM → `OFFER`.
  - `G7`: receipt_violation → `ACCEPT`.
  - `G8`: INR, $12 → `ACCEPT`, `fee == "0"`.
  - `G9`: CREDIT_NOT_PROCESSED, $50, refund_covers_amount → `FIGHT`, `evidence_type == "PROOF_OF_REFUND"`.
  - `G10`: DUPLICATE, distinct_duplicate → `FIGHT`.
  - `G11`: due_date = now - 1h → `EXPIRED`.
  - `G12`: disputes_90d=2, txns_90d=120 (1.67%) → `high_volume`, `hv_warning`, `fee == "30"` (non-SP, amount ≥ 30), due_at = created_at + 3 days.
  - Plus `test_hv_needs_over_100_txns` (2/100 → not high_volume), `test_warning_at_1pct` (2/150 → hv_warning, not high_volume), `test_p_clamped`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → all PASS (≥ 11/12 golden decisions is the acceptance bar; all 12 are expected with these priors).
- [ ] **Step 5: Commit** `feat: deterministic dispute economics with golden decision tests`.

---

### Task 11: Dispute core (ingest, evidence, playbooks, self-check, submit)

**Files:**
- Create: `app/disputes.py`, `corpus/playbooks/*.md` (7 files), `tests/test_disputes.py`, `tests/fixtures/disputes/*.json` (start from real JSON captured in `docs/verified.md`; redact names/emails)

**Interfaces:**
- Consumes: Tasks 2, 3, 5, 6, 10.
- Produces:
  - `async ingest(pool, pp, dispute_id: str, settings) -> dict`: GET dispute; upsert `disputes` (`capture_id` = `disputed_transactions[0].seller_transaction_id`, map to `orders` by `capture_id`; `due_at` from `seller_response_due_date` if present); returns `{"dispute","order","receipt","events","shipments"}` (any may be None or empty).
  - `async case_inputs(pool, pp, ctx: dict, settings, now: datetime) -> CaseInputs`: receipt strength via `verify_receipt` (with the live `custom_id` from `get_order`; `NONE` if no receipt); `receipt_violation` = cart total > `max_amount` or any constraint attribute differs from the cart; `constraints_match_cart` = has constraint attributes and none differ; refunds from the capture; counts from `orders` + `history_txns` + `disputes` in the last 90 days.
  - `async triage(pool, pp, dispute_id, settings, now=None) -> dict`: `ingest` + `case_inputs` + `decide`; upserts `cases` (state `NEW` if new) with `triage` (reason, stage, status, amount, due_at, hours_left, fee, sp_eligible, high_volume, hv_warning, dispute_rate_90d), `economics`, `receipt_strength`. Returns the case row.
  - `async gather_evidence(pool, pp, dispute_id) -> tuple[list[dict], list[str]]`: items `{"id": "E1".., "kind", "source", "summary", "data"}` with kinds `ORDER`, `CAPTURE`, `TRACKING`, `RECEIPT`, `RECEIPT_VERIFICATION`, `BUYER_MESSAGES`, `REFUND`, `ISSUE`; gaps per reason: INR needs `TRACKING`; SNAD needs `RECEIPT` or `BUYER_MESSAGES`; UNAUTHORIZED needs `RECEIPT`; CREDIT_NOT_PROCESSED needs `REFUND`. Stored on `cases.evidence` / `evidence_gaps`.
  - `load_playbook(reason: str, has_receipt: bool) -> dict` → `{"text", "policy_ids": set[str]}`; reads `corpus/playbooks/{reason}.md` (+ `AGENT_INTENT.md` if `has_receipt`); policy IDs are lines matching `^\[(POL-[A-Z]+-\d+)\]` and each has a `Source: https://...` line.
  - `class Draft(BaseModel)`: `action: Literal["PROVIDE_EVIDENCE","MAKE_OFFER","ACCEPT_CLAIM"]`, `evidence_type: str | None`, `note: str = Field(max_length=2000)`, `sections: list[Section]` (`heading`, `text`, `refs: list[str]`), `offer: Offer | None` (`offer_type`, `amount: str`).
  - `check_draft(draft: Draft, evidence: list[dict], policy_ids: set[str], economics: dict) -> list[str]`. Returns problems; empty means pass. Algorithm:
    ```
    corpus = json.dumps([e["data"] for e in evidence] + [e["summary"] for e in evidence])
    text   = draft.note + " " + " ".join(s.heading + " " + s.text for s in draft.sections)
    for tok in regex findall over text of:
        amounts  r"\$?\d+\.\d{2}"         -> strip "$", must be in corpus
        dates    r"\d{4}-\d{2}-\d{2}"      -> must be in corpus
        ids      r"\b(?=[A-Z0-9-]*\d)[A-Z0-9-]{8,}\b" -> skip tokens in evidence ids/policy ids; must be in corpus
    every section has >= 1 ref; every ref in {e["id"]} | policy_ids
    if draft.action == "MAKE_OFFER": offer.offer_type/amount == economics["offer"]
    action must match economics decision (FIGHT->PROVIDE_EVIDENCE, OFFER->MAKE_OFFER, ACCEPT->ACCEPT_CLAIM) unless cases.override says otherwise
    ```
    (ponytail: plain integers are not checked; extend the regex if drafts start inventing counts.)
  - `payload_for(draft: Draft, settings) -> dict`: PROVIDE_EVIDENCE → `{"input": {...evidences schema from docs/verified.md...}}`; MAKE_OFFER → `{"note","offer_type","offer_amount":{"currency_code":"USD","value"}}`; ACCEPT_CLAIM → `{"input": {"note"}}`. Real PII is restored here by the caller, not in the draft.
  - `payload_hash(payload: dict, pdf_sha256: str | None) -> str` = `sha256(canonical(payload) + (pdf_sha256 or "").encode()).hexdigest()`.
  - `ACTION_REL = {"PROVIDE_EVIDENCE": "provide_evidence", "MAKE_OFFER": "make_offer", "ACCEPT_CLAIM": "accept_claim"}` (**use the rel names recorded in `docs/verified.md`**).
  - `class ApprovalError(Exception)`.
  - `async submit_approved(pool, pp, dispute_id: str, action: str, payload_hash_: str) -> dict`: in one transaction, select the newest unconsumed approval for `(dispute_id, action, payload_hash_)` `FOR UPDATE` (none → `ApprovalError("no approval")`); check `cases.payload_hash == payload_hash_` and recompute the hash from `cases.payload` + `pdf_sha256` (mismatch → `ApprovalError`); GET the dispute; find the `ACTION_REL[action]` link (missing → set `NEEDS_ATTENTION`, raise `ApprovalError("action not allowed")`); POST it via `pp.post_link` (multipart with `evidence-file` = `cases.pdf` for PROVIDE_EVIDENCE; `input` part for ACCEPT_CLAIM; JSON for MAKE_OFFER) with `request_id = f"submit-{approval_id}"`; mark the approval consumed with `response` and `http_status`; set state `SUBMITTED` (or `ACCEPTED` for ACCEPT_CLAIM). If `cases.dry_run`, skip the POST and store `{"dry_run": true}`.
- Playbooks: one page each, plain guidance plus 2–4 `[POL-...]` lines quoting short PayPal policy facts with `Source:` URLs (HELP1083 etc. from the research plan §8 corpus table). `AGENT_INTENT.md` explains how to narrate the receipt (instruction, cap, constraints, hosted confirmation, PayPal approval, `custom_id` commitment).

- [ ] **Step 1: Write failing tests** (`db` + `FakePayPal` serving fixture JSON):
  - `test_ingest_maps_to_receipt` (fixture dispute whose `seller_transaction_id` equals a seeded order's `capture_id` → returns that receipt).
  - `test_triage_without_receipt_or_due_date` (Review Focus 1): unknown capture, no `seller_response_due_date` → case created, `receipt_strength == "NONE"`, `due_at == created + 10 days`.
  - `test_gaps_inr_without_tracking` → `"TRACKING"` in gaps.
  - `test_check_draft_catches_invented_amount` (draft mentions `$91.00` not in evidence → one problem), `test_check_draft_catches_unknown_ref`, `test_check_draft_offer_must_match_economics`, `test_check_draft_passes_clean_draft`.
  - `test_submit_without_approval_raises` (no PayPal POST made).
  - `test_submit_hash_mismatch_raises`.
  - `test_submit_once_on_double_approve` (Review Focus 4): one approval, `submit_approved` twice → first succeeds, second raises `ApprovalError`; `FakePayPal` saw exactly one POST to the action link.
  - `test_submit_missing_link_needs_attention`.
  - `test_dry_run_skips_post`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement `disputes.py` and write the playbooks. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: dispute ingest, evidence, self-check and approval-gated submit`.

---

### Task 12: Evidence pack PDF with QR

**Files:**
- Create: `app/pdf.py`, `tests/test_pdf.py`

**Interfaces:**
- Produces: `build_evidence_pdf(case: dict, draft: Draft, evidence: list[dict], receipt_view: dict | None, verify_url: str, cited_policy: list[dict], compress: bool = True) -> bytes`. Pages: cover (merchant name, dispute ID, reason, amount, "Prepared with Countersign", QR via `reportlab.graphics.barcode.qr.QrCodeWidget` encoding `verify_url`, plus the URL as text); timeline of receipt events (type, time, short hash); receipt block (seal hash, `custom_id`, signature check, strength); evidence items (summaries); draft sections with refs; cited PayPal policy lines with source URLs. `cited_policy` is only the `POL-*` lines referenced by the draft. Real PII appears only here (restored by the caller).

- [ ] **Step 1: Write failing tests** (with `compress=False` so text is searchable in the bytes): `test_pdf_header` (starts with `b"%PDF"`); `test_contains_dispute_and_verify_url`; `test_excludes_uncited_playbook_text` (a playbook sentence that was not cited is absent).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (`canvas.Canvas(buf, pageCompression=0 if not compress else 1)`). **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: evidence pack pdf with verify qr`.

---

### Task 13: Deep Agent (tools, fact-checker, interrupt, run/resume) + spike S8 part 2

**Files:**
- Create: `app/agent.py`, `scripts/spikes/s8_deepagent.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: Tasks 8, 10, 11, 12.
- Produces:
  - `SYSTEM_PROMPT` (constant): role (dispute analyst for a small PayPal seller); facts only from tool outputs; never invent numbers, dates, IDs or policy; follow `compute_economics`; work order: `get_case_facts` → `get_order_evidence` → `verify_receipt` → `compute_economics` → `load_playbook` → write the `Draft` → ask the `fact-checker` subagent → fix → `build_evidence_pack` → `submit_dispute_response(action, payload_hash)`; buyer data appears as tokens like `<EMAIL_1>`; write for a PayPal reviewer: concise, factual, chronological.
  - `FACT_CHECKER = {"name": "fact-checker", "description": "Checks a dispute draft against the evidence and lists unsupported claims.", "system_prompt": ..., "tools": [get_order_evidence, verify_receipt, check_draft]}`.
  - `make_tools(state, dispute_id: str) -> list` returning tools bound to that dispute (no `dispute_id` argument, so the agent cannot touch another case): `get_case_facts()`, `get_order_evidence()`, `verify_receipt()`, `compute_economics()`, `load_playbook()`, `check_draft(draft: Draft) -> list[str]`, `build_evidence_pack(draft: Draft) -> dict` (runs `check_draft`; on problems returns them without saving; otherwise saves `draft`, `check_issues=[]`, `payload` (`payload_for`), PDF (FIGHT only), `pdf_sha256`, `payload_hash` on `cases`; returns `{"payload_hash", "action"}`), `submit_dispute_response(action: str, payload_hash: str) -> dict` (calls `disputes.submit_approved`). Every read tool returns `Redactor.redact(...)` output; one `Redactor` per run.
  - `build_agent(state, dispute_id: str, model=None)` → `create_deep_agent(model=model or chat_model(settings), tools=make_tools(...), system_prompt=SYSTEM_PROMPT, subagents=[FACT_CHECKER with bound tools], interrupt_on={"submit_dispute_response": {"allowed_decisions": ["approve", "edit", "reject"]}}, checkpointer=state.checkpointer)`.
  - `async run_case(state, dispute_id: str, force: bool = False) -> str`: atomically `UPDATE cases SET state='ANALYZING', thread_id=... WHERE dispute_id=%s AND state IN ('NEW','NEEDS_ATTENTION'[, any if force]) RETURNING *` (no row → return the current state without running; this is the Review Focus 3 guard); `thread_id = f"{dispute_id}:{uuid4().hex[:8]}"`; `ainvoke` with message `f"Handle PayPal dispute {dispute_id}."`; if `__interrupt__`, store `pending_action = action_requests[0]` and set `AWAITING_APPROVAL`; if it finished without interrupting, set `NEEDS_ATTENTION` with `error = "agent finished without a submission"`; any exception → `NEEDS_ATTENTION` with `error = repr(e)[:500]`, other columns untouched. Returns the final state.
  - `async resume_case(state, dispute_id: str, decision: dict) -> str`: `ainvoke(Command(resume={"decisions": [decision]}), config with the stored thread_id)`; on a new interrupt → `AWAITING_APPROVAL` with the new `pending_action`; on completion the submit tool has already set the state; exceptions → `NEEDS_ATTENTION`.
  - `state.checkpointer`: `AsyncPostgresSaver.from_conn_string(database_url)` opened in the app lifespan, `await checkpointer.setup()` once.
- `scripts/spikes/s8_deepagent.py`: a deep agent with one tool `echo(text)` gated by `interrupt_on`, Postgres checkpointer; run → expect `__interrupt__`; resume with approve → tool result in the final message. Run on each keyed provider; record in `docs/verified.md`.

- [ ] **Step 1 (spike):** Write and run `s8_deepagent.py` for Gemini (and Groq/Nebius if keyed). If the OpenAI-compatible route fails for a provider, switch `chat_model` to its native LangChain package and record it.
- [ ] **Step 2: Write failing tests** (`MemorySaver` checkpointer; scripted fake model = `GenericFakeChatModel` subclass with `bind_tools` returning `self`, fed `AIMessage`s whose `tool_calls` walk the work order and end with `submit_dispute_response`; `db` + `FakePayPal` with a G1-like fixture):
  - `test_run_pauses_before_write`: `run_case` → state `AWAITING_APPROVAL`, `pending_action["name"] == "submit_dispute_response"`, and `FakePayPal` saw no POST to any dispute action link.
  - `test_reject_never_posts`: `resume_case(..., {"type": "reject", "message": "no"})` → no action POST.
  - `test_approve_without_approval_row_fails_safe`: `resume_case(..., {"type": "approve"})` with no `approvals` row → `ApprovalError` inside the tool → state `NEEDS_ATTENTION`, no POST.
  - `test_llm_failure_needs_attention` (Review Focus 5): the fake model raises `RuntimeError("429")` → state `NEEDS_ATTENTION`, `cases.triage` still set.
  - `test_tools_redact_pii`: `get_case_facts()` output contains no fixture email.
  - `test_run_case_guard`: case already `ANALYZING` → `run_case` returns `"ANALYZING"` and the model is never called.
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement `agent.py`. **Step 5:** Run → PASS.
- [ ] **Step 6: Commit** `feat: deep agent dispute analyst with fact-checker and approval interrupt`.

---

### Task 14: Case and approval HTTP routes

**Files:**
- Modify: `app/api.py`
- Create: `tests/test_api_cases.py`

**Interfaces:**
- Consumes: Tasks 11, 12, 13.
- Produces (all seller-only):
  - `GET /api/cases?state=&reason=&due_before=` → rows: `dispute_id, reason, stage, status, amount, currency, due_at, hours_left, fee, sp_eligible, receipt_strength, evidence_completeness` (1 − gaps/required), `decision`, `ev_saving` (accept − chosen EV), `state, outcome, dry_run`. Sorted by `due_at`.
  - `GET /api/cases/{id}` → `{dispute (redacted raw), case, evidence, evidence_gaps, draft, check_issues, economics, receipt_verification, pending_action, payload_hash, pdf_url, allowed_actions}` (`allowed_actions` = rels present in the stored dispute `links`).
  - `POST /api/cases/{id}/analyze {force?: bool}` → schedules `run_case` as an asyncio task → `{"state"}`.
  - `PUT /api/cases/{id}/draft` body `Draft` → runs `check_draft`; on problems 422 `{"problems"}`; else saves draft, rebuilds payload/PDF/hash → `{"payload_hash"}`.
  - `POST /api/cases/{id}/approve {action, payload_hash}` → 409 if `payload_hash != cases.payload_hash` or `check_issues` is non-empty; else insert an `approvals` row (`user_id="seller"`) and `resume_case` with `{"type":"approve"}` if `pending_action.args.payload_hash == payload_hash`, else `{"type":"edit","edited_action":{"name":"submit_dispute_response","args":{"action": action, "payload_hash": payload_hash}}}` → `{"state"}`.
  - `POST /api/cases/{id}/override {decision, reason}` → store `override`; if paused, `resume_case` with `{"type":"reject","message": f"Seller changed the decision to {decision}: {reason}. Redo the draft for {decision}."}`.
  - `POST /api/cases/{id}/reject` → `resume_case` reject; state `MANUAL`.
  - `GET /api/cases/{id}/pdf` → `application/pdf`.
  - `GET /api/metrics` → `{dispute_rate_90d, txns_90d, hv_threshold: 0.015, warn_threshold: 0.010, open_cases, due_48h, won, lost, dollars_defended, simulated_history: true|false}`.

- [ ] **Step 1: Write failing tests**: `test_edit_changes_hash_and_voids_old_approval` (approve with the old hash after a draft edit → 409); `test_approve_blocked_with_check_issues` (409); `test_approve_resumes_with_edit_when_hash_changed` (the agent resume is called with an `edit` decision; monkeypatch `resume_case`); `test_cases_sorted_by_due`; `test_metrics_counts_history`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: case queue, draft edit, approve, override and metrics routes`.

---

### Task 15: Webhooks and background loops

**Files:**
- Create: `app/webhooks.py`, `app/loops.py`, `tests/test_webhooks.py`
- Modify: `app/main.py` (router; start loops in the lifespan unless `settings` disables them for tests)

**Interfaces:**
- Produces:
  - `POST /webhooks/paypal`: reads the raw body; `verified = await pp.verify_webhook(headers, event, settings.paypal_webhook_id)`; if not verified and not `allow_simulated_webhooks` → 400; `INSERT INTO webhook_events ... ON CONFLICT (id) DO NOTHING RETURNING id` (no row → return `{"ok": true, "duplicate": true}`); for `CUSTOMER.DISPUTE.*` schedule `handle_dispute_event(state, event)`; return 200 at once.
  - `async handle_dispute_event(state, event)`: `dispute_id = event["resource"]["dispute_id"]`; `triage(...)`; on `CREATED`/`UPDATED` call `run_case` (guarded); on `RESOLVED` set `outcome` from the outcome field recorded in `docs/verified.md`: contains `SELLER` → `WON`, contains `BUYER` → `LOST`. Marks `processed_at`.
  - `loops.poll_once(state)`: `list_disputes({"page_size": 50})`; for each dispute whose `update_time` differs from the stored row, run `handle_dispute_event` with a synthetic `{"event_type": "CUSTOMER.DISPUTE.UPDATED", "resource": {...}}`.
  - `loops.sweep_once(state, now)`: cases not in `SUBMITTED|WON|LOST|ACCEPTED|EXPIRED|MANUAL` whose `disputes.due_at < now` → `EXPIRED`.
  - `loops.run_forever(state)`: poll every 300 s, sweep every 600 s, each wrapped in try/except with logging.

- [ ] **Step 1: Write failing tests**: `test_valid_event_processed` (fake verify returns SUCCESS → 200, row stored); `test_unverified_rejected` (400); `test_unverified_allowed_with_flag`; `test_duplicate_ignored` (same event ID twice → second response `duplicate: true`, handler scheduled once); `test_webhook_and_poll_start_one_run` (Review Focus 3: call `handle_dispute_event` and `poll_once` for the same new dispute; monkeypatched agent run counter == 1); `test_resolved_sets_won`; `test_sweep_expires_past_due`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: verified paypal webhooks, dispute poller and expiry sweeper`.

---

### Task 16: Sandbox simulator, judge demo, seed and reset

**Files:**
- Create: `app/sim.py` (router), `scripts/seed.py`, `scripts/demo_reset.py`, `tests/test_sim.py`
- Modify: `app/main.py` (include `sim` router)

**Interfaces:**
- Consumes: the dispute-creation path chosen in `docs/verified.md` (S3), Tasks 6, 11, 13.
- Produces (seller-only; 404 unless `simulator_enabled`):
  - `POST /api/sim/disputes {capture_id, reason, amount, buyer_email}` → `{"dispute_id"}` via the S3 path; 501 with `{"detail": "create the dispute as the buyer in the sandbox Resolution Center"}` if only the UI path works.
  - `POST /api/sim/disputes/{id}/require-evidence {action}` and `POST /api/sim/disputes/{id}/adjudicate {outcome}` → call the dispute's `links[]` (404 if the link is missing).
  - `POST /api/demo/run` (404 unless `demo_enabled`): if S3 found an automatic path → card order (S2 body) → capture → track → create dispute → returns `{"dispute_id","mode":"live"}`. Otherwise take the oldest unused dispute ID from `DEMO_DISPUTE_POOL` (env, comma list) that has no `cases` row → `triage` + `run_case` → `{"mode":"pool"}`; pool exhausted → re-run the most recent case with `dry_run = true` and a fresh thread → `{"mode":"dry_run"}`.
  - `scripts/seed.py`: idempotent. 12 products (handmade apparel and home goods, incl. `THROW-SAGE` "Linen throw – sage" $78 with `{"color":"sage"}`, plus sizes for SNAD cases); 120 `history_txns` over 90 days with 1 disputed (0.83%), `simulated = true`; prints the next manual steps (approve links) for live orders.
  - `scripts/demo_reset.py`: deletes `cases`, `approvals` (dispute ones), `webhook_events`, checkpoint tables; does not touch receipts.
- [ ] **Step 1: Write failing tests**: `test_sim_routes_404_when_disabled`; `test_demo_pool_then_dry_run` (pool of one ID → first call `mode == "pool"`, second `mode == "dry_run"`, and the dry-run case has `dry_run == true`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: sandbox simulator, judge demo and seed scripts`.

---

### Task 17: `report_issue` pre-dispute resolution (add-on)

**Files:**
- Create: `app/issues.py`, `tests/test_issues.py`
- Modify: `app/mcp_server.py` (2 tools), `app/api.py` (2 routes)

**Interfaces:**
- Produces:
  - `async report_issue(state, mandate_id, issue_type, description, desired_resolution) -> dict`: `issue_type` in `WRONG_ITEM|DAMAGED|NOT_RECEIVED|NOT_AS_DESCRIBED|OTHER`; `desired_resolution` in `REFUND|PARTIAL_REFUND|REPLACEMENT|RETURN`; requires a `CAPTURED` receipt (else error dict); id `"iss_" + token_hex(8)`; appends `ISSUE_REPORTED`; computes `proposal` deterministically: `REPLACEMENT` → `{"type":"REPLACEMENT"}`; `REFUND` or `RETURN` → full refund of the capture amount; `PARTIAL_REFUND` or `OTHER` → `round(0.4 × amount, 2)`; adds `summary` from `json_call(chat_model(fast=True), ...)` over the **redacted** description (on LLM failure the summary is the description truncated to 200 chars); state `PROPOSED`. Returns `{"issue_id","state","proposal"}`.
  - `async approve_issue(state, issue_id, payload_hash) -> dict`: the seller approves the current proposal. `payload_hash = sha256(canonical(proposal))` must match; inserts an `approvals` row (`action` `REFUND` or `REPLACEMENT`); state `OFFERED`; appends `ISSUE_OFFERED {proposal}`. No money moves yet.
  - `async respond_to_proposal(state, issue_id, agent_id, decision, counter_amount=None) -> dict` (buyer agent; the agent ID from the MCP key must match the receipt's `agent_id`): `ACCEPT` → consume the approval; `REFUND` → `pp.refund_capture(capture_id, amount, request_id=f"issue-{issue_id}", note=...)`; state `RESOLVED`; append `ISSUE_RESOLVED {type, amount?, refund_id?}`. `DECLINE` → state `REJECTED`. `COUNTER` (allowed once; `counter_amount` ≤ capture amount) → new proposal with that amount, state `PROPOSED` (back to the seller, whose earlier approval no longer matches), append `ISSUE_COUNTERED`. A second ACCEPT → error, no second refund.
  - MCP tools `report_issue(mandate_id, issue_type, description, desired_resolution)`, `respond_to_proposal(issue_id, decision, counter_amount: str | None = None)` and `get_issue_status(issue_id)`; routes `GET /api/issues` (seller) and `POST /api/issues/{id}/approve {payload_hash}` (seller).
- [ ] **Step 1: Write failing tests**: `test_partial_refund_proposal_40pct` ($84 → `"33.60"`); `test_no_refund_until_buyer_accepts` (seller approve → no refund POST; buyer ACCEPT → exactly one, with `PayPal-Request-Id == "issue-<id>"`); `test_accept_twice_refunds_once`; `test_counter_once_then_back_to_seller` (COUNTER → state `PROPOSED`, the old approval hash is rejected, a second COUNTER errors); `test_other_agent_cannot_respond`; `test_issue_events_on_receipt` (`ISSUE_REPORTED`, `ISSUE_OFFERED`, `ISSUE_RESOLVED` present and the chain still verifies); `test_llm_failure_falls_back_to_description`; `test_mcp_lists_eight_tools`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat: report_issue pre-dispute resolution via mcp`.

---

### Task 18: AP2 export (add-on)

**Files:**
- Modify: `app/receipts.py` (add `ap2_export`), `app/api.py` (route), `tests/test_receipts.py`

**Interfaces:**
- Produces: `ap2_export(receipt: dict, events: list[dict], order: dict | None, key, kid: str, merchant_name: str) -> dict` → `{"intent_mandate": {...}, "cart_mandate": {"contents": {...}, "merchant_authorization": jws_eddsa(key, contents, kid)}, "payment_mandate": {...} | None}`; route `GET /api/receipts/{id}/ap2` (public).
- [ ] **Step 1:** Fetch the official `google-agentic-commerce/AP2` repo's Python mandate types (`IntentMandate`, `CartContents`, `CartMandate`, `PaymentMandateContents`, `PaymentMandate`) and write their exact field names into the docstring of `ap2_export`. Map: instruction → natural-language description; constraints → merchants/SKUs/expiry; `user_cart_confirmation_required: true`; cart → contents with a W3C PaymentRequest-style `details.total`; PayPal order/capture → payment mandate contents; `user_authorization: null`.
- [ ] **Step 2: Write failing tests**: `test_ap2_cart_signature_verifies` (verify `merchant_authorization` with the public key over its signing input); `test_ap2_fields_present` (assert the official field names from Step 1 are keys); `test_ap2_payment_mandate_none_before_order`.
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement. **Step 5:** Run → PASS.
- [ ] **Step 6: Commit** `feat: export receipts as ap2-shaped signed mandates`.

---

### Task 19: LLM eval suite (golden cases end to end)

**Files:**
- Create: `tests/test_eval_llm.py` (marked `llm`), `tests/fixtures/golden/G1..G12.json`, `docs/eval-results.md`

**Interfaces:**
- Consumes: Tasks 10, 11, 13 with a real model (`LLM_*` env pointing at Nebius).
- Each golden fixture: dispute JSON + seeded order/receipt/shipments + expected `{decision, required_evidence_kinds}`.
- [ ] **Step 1:** Write the 12 fixtures (same scenarios as Task 10).
- [ ] **Step 2:** Write `test_golden_case[G1..G12]` (parametrized): seed, `triage`, `run_case` → for non-EXPIRED: state `AWAITING_APPROVAL`; `cases.draft.action` matches the decision; required evidence IDs are cited; `check_issues == []`; no fixture PII in any message in the checkpoint state. G11 → state `EXPIRED`, no agent run.
- [ ] **Step 3:** Run `uv run --env-file .env pytest -m llm -v`; record pass count, total tokens and cost (LangSmith if enabled) in `docs/eval-results.md`. Target: ≥ 11/12 decisions, 0 unsupported claims, 0 PII leaks.
- [ ] **Step 4: Commit** `test: llm golden eval suite and results`.

---

### Task 20: Deploy to Render, webhook spike S6, README, end-to-end run

**Files:**
- Create: `render.yaml`, `README.md`
- Modify: `docs/verified.md`

- [ ] **Step 1:** `render.yaml`: one `web` service (`runtime: python`, `buildCommand: pip install uv && uv sync --frozen`, `startCommand: uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT`, `healthCheckPath: /healthz`, env vars from design §15 with `sync: false`). Database: Neon `DATABASE_URL` (Render free Postgres expires during judging).
- [ ] **Step 2 (owner):** Deploy the blueprint on Render (paid instance on sponsor credits so it never sleeps); set env vars; register the webhook at `https://<service>/webhooks/paypal` for `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, `CUSTOMER.DISPUTE.RESOLVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`; put the webhook ID into `PAYPAL_WEBHOOK_ID`.
- [ ] **Step 3 (S6):** Trigger a real sandbox event (capture an order); confirm a `webhook_events` row with `verified = true`. Record in `docs/verified.md`. If verification fails, keep the poller as the main path and record why.
- [ ] **Step 4:** `uv run python scripts/seed.py` against the hosted DB.
- [ ] **Step 5 (E2E, hosted):** `buyer_agent.py` → open the confirm URL → confirm via `POST /api/mandates/{id}/confirm` → approve in PayPal → capture → track → create a dispute (S3 path) → webhook → agent → `AWAITING_APPROVAL` → approve → submitted → simulator adjudicate `SELLER_FAVOR` → case `WON`. Repeat once with an OFFER case. Note the LangSmith trace URL.
- [ ] **Step 6:** `README.md`: one-paragraph pitch; hosted URL; architecture diagram (mermaid); PayPal APIs used; a **Tools** section naming each AI model and provider and what it does (Devpost requires this); the Verifiable Intent / AP2 mapping table; how approval is enforced twice; run locally (`uv sync`, `.env`, `uv run uvicorn ...`); "Import into Postman" (link to the hosted `/openapi.json`); MCP client config (Claude Desktop / Inspector); eval results table; honesty notes (receipts are evidence, not a liability shift; agent-dispute volumes are forecast; simulated history is labelled; which dispute-creation path the demo uses); link to the frontend repo; MIT licence.
- [ ] **Step 7: Commit** `docs: render deploy, readme and verified e2e`. Confirm the GitHub repo's About shows the MIT licence.
- [ ] **Step 8 (submission, with the owner, after the frontend is done):** in the `Countersign` repo run `git subtree add --prefix backend https://github.com/Bhuvansai-16/Countersign-backend.git main` (later updates: `git subtree pull --prefix backend ... main`), so the submitted repo contains all source code as the rules require. Point Render's `rootDir` at `backend/` only if deploying from that repo.

---

## Order and timing (IST)

| When | Tasks |
|---|---|
| Nov 4 eve | 0 (owner), 1, 2, 3 |
| Nov 5 | 4 (spikes), 5 |
| Nov 6 | 6, 7, 8, 9 |
| Nov 7 | 10, 11, 12 |
| Nov 8 | 13, 14, 15, 20 (deploy early for S6) |
| Nov 9 morning | 16, 17, 18; then frontend begins |
| Nov 10–11 (alongside frontend) | 19, final E2E |
