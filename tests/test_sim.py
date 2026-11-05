import base64
import json
from dataclasses import replace

import httpx
import pytest

from app import agent as ag
from app import db as dbmod
from app.auth import make_token
from app.main import create_app
from tests.conftest import FakePayPal
from tests.test_disputes import BASE, dispute_json, pp_for

pytestmark = pytest.mark.anyio


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def auth(settings):
    return {"Authorization": "Bearer " + make_token(settings.session_secret)}


async def test_sim_routes_404_when_disabled(db, settings):
    app = create_app(settings, pool=db, paypal=FakePayPal().client)
    async with client(app) as c:
        assert (await c.post("/api/sim/disputes/PP-D-1001/adjudicate", headers=auth(settings), json={})).status_code == 404
        assert (await c.post("/api/demo/run", headers=auth(settings))).status_code == 404


async def test_sim_action_uses_paypal_link(db, settings):
    d = dispute_json(status="UNDER_REVIEW")
    d["links"].append({"rel": "adjudicate", "href": BASE + "/adjudicate", "method": "POST"})
    pp = pp_for(d)
    app = create_app(replace(settings, simulator_enabled=True), pool=db, paypal=pp.client)
    async with client(app) as c:
        r = await c.post("/api/sim/disputes/PP-D-1001/adjudicate", headers=auth(settings),
                         json={"adjudication_outcome": "SELLER_FAVOR"})
    assert r.status_code == 200
    sent = pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/adjudicate")
    assert json.loads(sent[0].content) == {"adjudication_outcome": "SELLER_FAVOR"}


async def test_sim_action_missing_link_404(db, settings):
    pp = pp_for(dispute_json())
    app = create_app(replace(settings, simulator_enabled=True), pool=db, paypal=pp.client)
    async with client(app) as c:
        r = await c.post("/api/sim/disputes/PP-D-1001/adjudicate", headers=auth(settings), json={})
    assert r.status_code == 404 and "adjudicate" in r.json()["detail"]


async def test_sim_create_dispute_sends_auth_assertion(db, settings):
    pp = FakePayPal({("POST", "/v1/customer/disputes"): {"links": [{"rel": "self", "href": BASE}]}})
    s = replace(settings, simulator_enabled=True)
    app = create_app(s, pool=db, paypal=pp.client)
    async with client(app) as c:
        r = await c.post("/api/sim/disputes", headers=auth(settings), json={
            "buyer_transaction_id": "B1", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "amount": "84.00",
            "buyer_email": "buyer@personal.example.com"})
    assert r.json() == {"dispute_id": "PP-D-1001"}
    sent = pp.calls_to("POST", "/v1/customer/disputes")[0]
    header, payload, _ = sent.headers["PayPal-Auth-Assertion"].split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {"alg": "none"}
    assert json.loads(base64.urlsafe_b64decode(pad(payload))) == {"iss": s.paypal_client_id, "email": "buyer@personal.example.com"}
    assert b'"buyer_transaction_id": "B1"' in sent.content


async def test_demo_pool_then_dry_run(db, settings, monkeypatch):
    runs = []

    async def fake_run(state, dispute_id, force=False, model=None):
        runs.append((dispute_id, force))
        return "ANALYZING"

    monkeypatch.setattr(ag, "run_case", fake_run)
    pp = pp_for(dispute_json())
    app = create_app(replace(settings, demo_enabled=True, demo_dispute_pool=("PP-D-1001",)), pool=db, paypal=pp.client)
    async with client(app) as c:
        first = (await c.post("/api/demo/run", headers=auth(settings))).json()
        await dbmod.execute(db, "UPDATE cases SET state = 'WON', outcome = 'RESOLVED_SELLER_FAVOUR'")
        second = (await c.post("/api/demo/run", headers=auth(settings))).json()
    assert first == {"dispute_id": "PP-D-1001", "mode": "pool"}
    assert second == {"dispute_id": "PP-D-1001", "mode": "dry_run"}
    case = await dbmod.fetchrow(db, "SELECT dry_run, state, outcome FROM cases")
    assert case == {"dry_run": True, "state": "NEW", "outcome": None}
    assert [r[0] for r in runs] == ["PP-D-1001", "PP-D-1001"]
