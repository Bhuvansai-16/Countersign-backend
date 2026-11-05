# LLM eval results

Model: `sarvam-105b` via `https://api.sarvam.ai/v1`. Run: 2026-11-05 10:30 UTC. Command: `uv run --env-file .env pytest -m llm -v`.

| Case | Scenario | Expected | Agent action | Self-check issues | PII leak | Main-agent tokens (in/out) | Seconds | Pass |
|---|---|---|---|---|---|---|---|---|
| G1 | INR, tracking delivered, Seller Protection | FIGHT | PROVIDE_EVIDENCE | 0 | none | 34317/10039 | 238 | yes |

**1/1 passed.** Token counts cover the main agent only (the fact-checker subagent runs in its own context). Decisions for all 12 golden cases are also checked deterministically in tests/test_economics.py.
