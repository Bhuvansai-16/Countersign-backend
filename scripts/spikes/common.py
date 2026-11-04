"""Shared helpers for sandbox spike scripts. Run from the repo root: uv run --env-file .env python scripts/spikes/<name>.py"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.paypal import PayPalClient  # noqa: E402

VERIFIED = ROOT / "docs" / "verified.md"


def settings():
    return load_settings()


def paypal(s=None):
    s = s or settings()
    return PayPalClient(s.paypal_base_url, s.paypal_client_id, s.paypal_client_secret)


def record(title: str, result: str, details: dict | str):
    """Append a spike result to docs/verified.md (no secrets: callers pass responses, never tokens)."""
    body = details if isinstance(details, str) else json.dumps(details, indent=2, default=str)
    if not VERIFIED.exists():
        VERIFIED.write_text("# Sandbox verification results\n\nRecorded by scripts/spikes/*.py.\n", encoding="utf-8")
    with VERIFIED.open("a", encoding="utf-8") as f:
        f.write(f"\n## {title} - {result}\n\n_{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC_\n\n```json\n{body}\n```\n")
    print(f"{title}: {result}")
