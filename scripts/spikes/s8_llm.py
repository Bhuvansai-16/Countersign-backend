"""S8 part 1: list models on the configured OpenAI-compatible endpoint and make one JSON-mode call."""
import asyncio

import httpx
from pydantic import BaseModel

from common import record, settings

from app.llm import chat_model, json_call


class Ok(BaseModel):
    ok: bool


async def main():
    s = settings()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(s.llm_base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {s.llm_api_key}"})
    ids = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
    try:
        out = await json_call(chat_model(s), "You are a test.", 'Return {"ok": true}.', Ok)
        result = "PASS" if out.ok else "FAIL"
    except Exception as e:  # spike: record whatever happened
        out, result = repr(e)[:300], "FAIL"
    record(f"S8 LLM json mode ({s.llm_model} @ {s.llm_base_url})", result,
           {"models_status": r.status_code, "model_ids_sample": ids[:30], "json_call": str(out)})


asyncio.run(main())
