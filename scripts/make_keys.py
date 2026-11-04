"""Print a fresh merchant signing key line for .env."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.receipts import new_signing_seed  # noqa: E402

print(f"MERCHANT_SIGNING_KEY={new_signing_seed()}")
