import asyncio
import json as jsonlib
import time
import uuid
from typing import Any, Mapping

import httpx


class PayPalError(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        self.name = body.get("name") if isinstance(body, dict) else None
        super().__init__(f"PayPal {status} {self.name or ''}".strip())


class PayPalClient:
    def __init__(self, base_url: str, client_id: str, secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._auth = (client_id, secret)
        self._http = httpx.AsyncClient(transport=transport, timeout=30)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            r = await self._http.post(
                f"{self.base_url}/v1/oauth2/token", auth=self._auth, data={"grant_type": "client_credentials"}
            )
            if r.status_code >= 400:
                raise PayPalError(r.status_code, _body(r))
            body = r.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 0))
            return self._token

    def token_ttl(self) -> int:
        return max(0, int(self._expires_at - time.time())) if self._token else 0

    async def request(
        self,
        method: str,
        path_or_url: str,
        *,
        json: Any = None,
        files: dict | None = None,
        data: dict | None = None,
        request_id: str | None = None,
        mock: str | None = None,
        prefer_representation: bool = False,
        headers: dict | None = None,
    ) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        headers = dict(headers or {})
        if method == "POST" and request_id is None:
            request_id = str(uuid.uuid4())
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if mock:
            headers["PayPal-Mock-Response"] = jsonlib.dumps({"mock_application_codes": mock})
        if prefer_representation:
            headers["Prefer"] = "return=representation"
        for attempt in range(2):
            headers["Authorization"] = f"Bearer {await self.token()}"
            r = await self._http.request(method, url, json=json, files=files, data=data, headers=headers)
            if r.status_code == 401 and attempt == 0:
                self._token = None  # stale token: refresh once, same request id
                continue
            break
        if r.status_code >= 400:
            raise PayPalError(r.status_code, _body(r))
        return r.json() if r.content else {}

    # Orders v2
    async def create_order(self, body: dict, request_id: str) -> dict:
        return await self.request("POST", "/v2/checkout/orders", json=body, request_id=request_id, prefer_representation=True)

    async def get_order(self, order_id: str) -> dict:
        return await self.request("GET", f"/v2/checkout/orders/{order_id}", prefer_representation=True)

    async def capture_order(self, order_id: str, request_id: str, mock: str | None = None) -> dict:
        return await self.request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", request_id=request_id, mock=mock, prefer_representation=True
        )

    async def track_order(self, order_id: str, body: dict, request_id: str) -> dict:
        return await self.request("POST", f"/v2/checkout/orders/{order_id}/track", json=body, request_id=request_id)

    async def update_tracker(self, order_id: str, tracker_id: str, patch: list[dict]) -> dict:
        return await self.request("PATCH", f"/v2/checkout/orders/{order_id}/trackers/{tracker_id}", json=patch)

    # Payments v2
    async def get_capture(self, capture_id: str) -> dict:
        return await self.request("GET", f"/v2/payments/captures/{capture_id}")

    async def refund_capture(self, capture_id: str, amount: str, request_id: str, note: str, currency: str = "USD") -> dict:
        body = {"amount": {"value": amount, "currency_code": currency}, "note_to_payer": note[:255]}
        return await self.request("POST", f"/v2/payments/captures/{capture_id}/refund", json=body, request_id=request_id)

    # Disputes v1
    async def list_disputes(self, params: dict) -> dict:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return await self.request("GET", "/v1/customer/disputes" + (f"?{query}" if query else ""))

    async def get_dispute(self, dispute_id: str) -> dict:
        return await self.request("GET", f"/v1/customer/disputes/{dispute_id}")

    async def post_link(self, href: str, *, json: Any = None, files: dict | None = None, data: dict | None = None, request_id: str) -> dict:
        return await self.request("POST", href, json=json, files=files, data=data, request_id=request_id)

    # Webhooks
    async def verify_webhook(self, headers: Mapping[str, str], event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        out = await self.request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return out.get("verification_status") == "SUCCESS"


def _body(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:500]}
