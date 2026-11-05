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
