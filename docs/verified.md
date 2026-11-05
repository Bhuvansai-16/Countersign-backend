# Sandbox verification results

Recorded by scripts/spikes/*.py.

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
