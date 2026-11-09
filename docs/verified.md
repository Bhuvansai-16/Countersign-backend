# Sandbox verification results

Recorded by scripts/spikes/*.py.

## US sandbox app and local runtime checkpoint - PASS (checkout still pending)

_2026-11-05, manually verified by Codex_

- Created merchant app **Countersign**, linked to US seller `sb-iaenv53656392@business.example.com`.
- Customer disputes enabled; Transaction search enabled and saved; JavaScript SDK v6 enabled.
- Client ID/secret replaced only in the git-ignored backend `.env`; no credentials recorded here.
- OAuth `POST /v1/oauth2/token`: **200**, token lifetime **32400 seconds**. Granted scopes include disputes
  `read-buyer`, `read-seller`, `create`, `update-seller`, reporting search, and shipping trackers.
- Seller `GET /v1/customer/disputes?page_size=10`: **200**, **0 items**.
- Backend restarted on **127.0.0.1:8010** with the US app; existing dev Postgres recovered on **50168** and `.env`
  updated accordingly. No application data reset. `PUBLIC_BASE_URL=http://localhost:8010`.
- `/healthz`: DB healthy and a valid PayPal token cached. Seller API products/cases/orders/issues/metrics: **200**.
- Runtime Trust Gate checks: a $10 cap on the $84 cart returns **422 OVER_CAP**; requiring red while selecting sage
  returns **422 ATTRIBUTE_MISMATCH**. The valid sage cart creates a $84 mandate under a $90 cap.
- Fresh buyer link: `http://localhost:3000/confirm/rcpt_68bf79004ca5cdd170749995`.
  Still **PENDING_CONFIRMATION**; expires **2026-11-06 11:46:29 UTC**. No US-app order/payment was created through
  this mandate yet: browser automation disconnected before the buyer confirmation and PayPal login steps.
- Verification: **154 backend tests passed**, **11 live LLM tests deselected**; frontend TypeScript and ESLint passed.

Remaining S1: buyer confirmation -> PayPal approval -> capture -> tracking -> live receipt verification. S3-S7 and
webhook delivery remain unverified. Old orders created under the India app have been preserved.

### S1 US app: frontend confirmation and PayPal redirect - PASS (payment pending)

_2026-11-05, browser retry_

The prepared confirmation page loaded correctly. Clicking **Confirm this purchase** created US-app PayPal order
`2NG228202S383952F`. **Pay with PayPal** reached the genuine sandbox checkout and Buyer A's password screen.
Password retrieval was blocked by automatic approval review; the owner was asked to enter the sandbox password
directly and approve the $84 payment. Capture and STRONG receipt verification are still pending that action.
Repeatable PowerShell startup and test instructions: `docs/RUN_LOCAL.md`.

Live `GET /api/receipts/rcpt_68bf79004ca5cdd170749995/verify` returned **200**: `chain_ok`, `seal_ok`, `sig_ok`,
and `custom_id_match` are all **true**. PayPal returned the full **68-character** custom ID. Receipt strength is
correctly **WEAK** while the mandate is **ORDERED** and buyer payment has not been approved/captured.

### S1 US app: buyer approval -> capture -> STRONG receipt - PASS (tracking pending)

_2026-11-05, owner approved Buyer A; Codex verified capture and receipt_

- Order `2NG228202S383952F`, amount **84.00 USD**, receipt `rcpt_68bf79004ca5cdd170749995`.
- Initial return page failed with a Next.js static-path worker error. `/login` was 200 while `/confirm/[id]` was
  500; dev logs showed the failed worker and EPIPE. Restarting the verified Countersign frontend restored 200.
- Capture then exposed **UNSUPPORTED_MEDIA_TYPE**: an empty httpx POST omitted Content-Type. Added a failing
  `test_capture_sends_json_content_type` regression, then fixed the shared capture method with `json={}`.
  PayPal's capture example also specifies `Content-Type: application/json`:
  https://developer.paypal.com/platforms/checkout/advanced/sdk/v1/
- Reloaded the same approved-order return URL after restarting the API. The page displayed **Paid. Thank you!**
  and capture **3L906348KK120212Y**. Seller orders API reports **COMPLETED**, **84.00 USD**, **STRONG**.
- Live receipt check: `chain_ok=true`, `seal_ok=true`, `sig_ok=true`, `custom_id_match=true`, **strength=STRONG**,
  `checked_against=paypal`. Seal hash: `9c76ad253d0c3fee7529d0756a1763c309e791f93066913419cfd57a14df7f1b`.
- Repeating `POST /api/orders/2NG228202S383952F/capture` returned 200 and the **same capture ID**.
- Final backend suite: **155 passed, 11 live LLM cases deselected**, 68.27 s. The first full run had 87 setup errors
  from embedded test PostgreSQL startup; recovered that existing test DB and reran successfully.

Tracking, dispute creation, evidence submission, adjudication and real webhook delivery remain unverified.

## S8 LLM json mode (sarvam-105b @ https://api.sarvam.ai/v1) - PASS

_2026-11-05 09:48 UTC_

```json
{
  "models_status": 200,
  "model_ids_sample": [
    "sarvam-105b",
    "sarvam-105b-conversations"
  ],
  "json_call": "ok=True"
}
```

## S8 Deep Agent interrupt/resume (sarvam-105b) - PASS

_2026-11-05 09:49 UTC_

```json
{
  "interrupted": true,
  "tool_args": {
    "text": "countersign"
  },
  "final": "\nI've acknowledged your instruction. How can I help you today?"
}
```

## S1 (partial): order create with 68-char custom_id via the frontend confirm page - PASS

_2026-11-05, India-region sandbox app (US app still to be created)_

Confirm on `http://localhost:3000/confirm/rcpt_cc9302e5a416b4bc619167c7` created sandbox order `24220106W24443329`.
`GET /api/receipts/{id}/verify` read the order back from PayPal: `custom_id` = `cs1:` + 64 hex (68 chars) accepted and
returned unchanged; chain_ok, seal_ok, sig_ok, custom_id_match all true; strength WEAK until the buyer approves
(PAYPAL_APPROVED) and the order is captured. Buyer approval, capture and tracking (rest of S1) still to run.

## S3a: create a sandbox dispute via API (PayPal-Auth-Assertion) - FAIL

_2026-11-09, US app._ `POST /v1/customer/disputes` with an unsigned `PayPal-Auth-Assertion` (`iss` = client ID, `email` =
Buyer A) for capture `3L906348KK120212Y` returned `invalid_request: No permissions to set target_client_id`.
The app lacks third-party (act-on-behalf) permission. Decision: create demo disputes as the buyer in the sandbox
Resolution Center (fallback c). The 4 captured US orders remain undisputed and usable.

## Database moved to Neon - PASS

_2026-11-09._ Dev data copied from the embedded Postgres to Neon (Postgres 18, AWS us-east-2) with
`pg_dump --column-inserts` loaded through psycopg (the bundled psql has no SSL). `DATABASE_URL` now points at Neon;
tests still use the embedded Postgres. Gotcha: the dump's `set_config('search_path', '')` leaked into a pooled Neon
session; fixed with `RESET search_path`. Backend health OK on Neon.
