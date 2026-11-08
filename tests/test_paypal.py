import httpx
import pytest

from app.paypal import PayPalError
from tests.conftest import FakePayPal

pytestmark = pytest.mark.anyio


async def test_token_cached():
    fake = FakePayPal({("GET", "/v2/checkout/orders/"): {"id": "O1"}})
    await fake.client.get_order("O1")
    await fake.client.get_order("O1")
    assert len(fake.calls_to("POST", "/v1/oauth2/token")) == 1


async def test_401_refreshes_once_same_request_id():
    fake = FakePayPal({
        ("POST", "/v2/checkout/orders"): [httpx.Response(401), httpx.Response(201, json={"id": "O1"})],
    })
    out = await fake.client.create_order({"intent": "CAPTURE"}, request_id="rid-9")
    assert out == {"id": "O1"}
    assert len(fake.calls_to("POST", "/v1/oauth2/token")) == 2
    order_calls = fake.calls_to("POST", "/v2/checkout/orders")
    assert len(order_calls) == 2
    assert {c.headers["PayPal-Request-Id"] for c in order_calls} == {"rid-9"}


async def test_post_has_request_id():
    fake = FakePayPal({("POST", "/v2/checkout/orders/O1/capture"): {"status": "COMPLETED"}})
    await fake.client.capture_order("O1", request_id="rid-1")
    assert fake.calls_to("POST", "/v2/checkout/orders/O1/capture")[0].headers["PayPal-Request-Id"] == "rid-1"


async def test_capture_sends_json_content_type():
    fake = FakePayPal({("POST", "/v2/checkout/orders/O1/capture"): {"status": "COMPLETED"}})
    await fake.client.capture_order("O1", request_id="rid-1")
    sent = fake.calls_to("POST", "/v2/checkout/orders/O1/capture")[0]
    assert sent.headers.get("Content-Type") == "application/json"


async def test_post_without_request_id_gets_one():
    fake = FakePayPal({("POST", "/v1/customer/disputes/D1/require-evidence"): {}})
    await fake.client.request("POST", "/v1/customer/disputes/D1/require-evidence", json={})
    assert fake.calls_to("POST", "/v1/customer/disputes/D1")[0].headers.get("PayPal-Request-Id")


async def test_mock_header():
    fake = FakePayPal({("POST", "/v2/checkout/orders/O1/capture"): {}})
    await fake.client.capture_order("O1", "r", mock="INSTRUMENT_DECLINED")
    sent = fake.calls_to("POST", "/v2/checkout/orders/O1/capture")[0]
    assert sent.headers["PayPal-Mock-Response"] == '{"mock_application_codes": "INSTRUMENT_DECLINED"}'


async def test_error_raises():
    fake = FakePayPal({
        ("GET", "/v1/customer/disputes/ERRDIS015"): httpx.Response(403, json={"name": "PERMISSION_DENIED"}),
    })
    with pytest.raises(PayPalError) as exc:
        await fake.client.get_dispute("ERRDIS015")
    assert exc.value.status == 403
    assert exc.value.name == "PERMISSION_DENIED"


async def test_post_link_uses_absolute_href():
    href = "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/make-offer"
    fake = FakePayPal({("POST", "/v1/customer/disputes/PP-D-1/make-offer"): {}})
    await fake.client.post_link(href, json={"note": "x"}, request_id="r")
    assert str(fake.calls_to("POST", "/v1/customer/disputes/PP-D-1/make-offer")[0].url) == href


async def test_verify_webhook_success():
    fake = FakePayPal({("POST", "/v1/notifications/verify-webhook-signature"): {"verification_status": "SUCCESS"}})
    headers = {
        "paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://c", "paypal-transmission-id": "t",
        "paypal-transmission-sig": "s", "paypal-transmission-time": "now",
    }
    assert await fake.client.verify_webhook(headers, {"id": "WH-1"}, "WEBHOOK-ID") is True
