"""Idempotent demo seed: 12 catalog products and 120 simulated historical transactions (1 disputed, 0.83%).

Usage (repo root): uv run --env-file .env python scripts/seed.py
History rows are marked simulated=true and the UI labels them "simulated history".
"""
import asyncio
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402

PRODUCTS = [
    ("THROW-SAGE", "Linen throw - sage", "Stonewashed linen throw, 130x170 cm", "78.00", "31.00", {"color": "sage", "material": "linen"}),
    ("THROW-BLUE", "Linen throw - blue", "Stonewashed linen throw, 130x170 cm", "78.00", "31.00", {"color": "blue", "material": "linen"}),
    ("SHIRT-LINEN-M", "Linen shirt - M", "Relaxed-fit linen shirt, natural", "64.00", "22.00", {"size": "M", "color": "natural"}),
    ("SHIRT-LINEN-L", "Linen shirt - L", "Relaxed-fit linen shirt, natural", "64.00", "22.00", {"size": "L", "color": "natural"}),
    ("APRON-OAT", "Canvas apron - oat", "Heavy canvas apron with leather straps", "42.00", "14.00", {"color": "oat"}),
    ("NAPKINS-4", "Cotton napkins, set of 4", "Block-printed cotton napkins", "28.00", "9.00", {"color": "indigo"}),
    ("MUG-SPECKLE", "Speckled stoneware mug", "Hand-thrown 350 ml mug", "24.00", "8.00", {"color": "speckle"}),
    ("BOWL-SET", "Stoneware bowl set", "Set of 3 nesting bowls", "56.00", "20.00", {"color": "ash"}),
    ("SCARF-WOOL", "Merino scarf", "Hand-loomed merino scarf", "48.00", "16.00", {"color": "rust"}),
    ("TOTE-WAXED", "Waxed canvas tote", "Waxed canvas tote with brass hardware", "58.00", "19.00", {"color": "olive"}),
    ("CANDLE-CEDAR", "Cedar soy candle", "Hand-poured soy candle, 220 g", "12.00", "4.00", {"scent": "cedar"}),
    ("CUSHION-ROSE", "Linen cushion cover - rose", "45x45 cm cushion cover", "36.00", "11.00", {"color": "rose"}),
]


async def main():
    pool = await db.open_pool(os.environ["DATABASE_URL"])
    await db.apply_schema(pool)
    for sku, name, desc, price, cost, attrs in PRODUCTS:
        await db.execute(pool, "INSERT INTO products (sku, name, description, price, cost, attributes) VALUES (%s,%s,%s,%s,%s,%s) "
                         "ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, "
                         "price = EXCLUDED.price, cost = EXCLUDED.cost, attributes = EXCLUDED.attributes",
                         sku, name, desc, price, cost, json.dumps(attrs))
    if (await db.fetchrow(pool, "SELECT count(*) AS n FROM history_txns"))["n"] == 0:
        rng, now = random.Random(7), datetime.now(timezone.utc)
        for i in range(120):
            await db.execute(pool, "INSERT INTO history_txns (at, amount, disputed, simulated) VALUES (%s, %s, %s, true)",
                             now - timedelta(days=rng.uniform(1, 89)), rng.choice([24, 36, 42, 56, 64, 78]), i == 0)
    n = await db.fetchrow(pool, "SELECT (SELECT count(*) FROM products) AS p, (SELECT count(*) FROM history_txns) AS h")
    print(f"seeded: {n['p']} products, {n['h']} simulated history transactions")
    print("next: create orders through the MCP buyer agent or POST /api/mandates, approve them in the PayPal sandbox, "
          "then open disputes (see docs/verified.md for the path that works).")
    await pool.close()


asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
