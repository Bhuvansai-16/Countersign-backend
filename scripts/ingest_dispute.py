"""Pull one PayPal dispute into Countersign now (instead of waiting for the 5-minute poller) and run the agent.

Usage (repo root): uv run --env-file .env python scripts/ingest_dispute.py PP-R-XXXX-NNNN
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.webhooks import handle_dispute_event  # noqa: E402


async def main(dispute_id: str) -> None:
    app = create_app(load_settings())
    async with app.router.lifespan_context(app):
        state = app.state.cs
        await handle_dispute_event(state, {"event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {"dispute_id": dispute_id}})
        from app import db
        case = await db.fetchrow(state.pool, "SELECT state, receipt_strength, economics->>'decision' AS decision, error "
                                             "FROM cases WHERE dispute_id = %s", dispute_id)
        print(dict(case) if case else "no case created")


asyncio.run(main(sys.argv[1]), loop_factory=asyncio.SelectorEventLoop)
