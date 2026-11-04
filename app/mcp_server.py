"""Countersign MCP server for buyer agents: quote and mandate, never pay."""

import json
from contextvars import ContextVar

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from app import auth
from app import db as dbmod
from app import mandates as m

# Set by the authenticated /mcp mount from the agent's API key; tools never take the agent id as an argument.
current_agent: ContextVar[str | None] = ContextVar("current_agent", default=None)

POLICY = (
    "Countersign agent policy: agents may search the catalog and create a mandate (a quote within the "
    "principal's cap). Agents never pay or confirm: a human confirms on the hosted page and approves in PayPal. "
    "Every step is recorded in a hash-chained receipt, signed by the merchant, and its seal hash is written into "
    "the PayPal order's custom_id before payment."
)


def _agent() -> str:
    agent = current_agent.get()
    if not agent:
        raise PermissionError("missing agent identity (send Authorization: Bearer <agent key>)")
    return agent


def build_mcp(state) -> MCPServer:
    server = MCPServer("countersign", instructions=POLICY)

    @server.tool()
    async def search_products(query: str = "", max_price: float | None = None) -> dict:
        """Search the merchant catalog by name, description or attribute. Prices are USD."""
        _agent()
        q = query.lower()
        rows = await dbmod.fetch(state.pool, "SELECT * FROM products ORDER BY sku")
        hits = [p for p in m.products_json(rows)
                if (not q or q in (p["name"] + " " + p["description"] + " " + json.dumps(p["attributes"])).lower())
                and (max_price is None or float(p["price"]) <= max_price)]
        return {"products": hits}

    @server.tool()
    async def create_mandate(instruction: str, max_amount: str, items: list[dict], attributes: dict | None = None,
                             currency: str = "USD", agent_platform: str = "custom",
                             agent_public_key: str | None = None, agent_sig: str | None = None) -> dict:
        """Create a purchase mandate: your principal's instruction, spend cap and required attributes, plus the
        cart (items: [{sku, qty}]). Returns a quote and a confirm_url to give to the human. You cannot pay."""
        try:
            return await m.create_mandate(
                state.pool, state.settings, instruction=instruction, max_amount=max_amount, currency=currency,
                attributes=attributes or {}, items=items, agent_id=_agent(), agent_platform=agent_platform,
                agent_public_key=agent_public_key, agent_sig=agent_sig)
        except m.MandateError as e:
            if e.violations:
                return {"error": "VIOLATIONS", "detail": e.detail, "violations": e.violations}
            return {"error": "REJECTED", "status": e.status, "detail": e.detail}

    @server.tool()
    async def get_mandate_status(mandate_id: str) -> dict:
        """Poll after giving the human the confirm_url: status, PayPal order, approve link and capture id."""
        _agent()
        return await m.mandate_status(state.pool, mandate_id)

    @server.tool()
    async def cancel_mandate(mandate_id: str) -> dict:
        """Cancel a mandate that the human has not confirmed yet."""
        _agent()
        return await m.cancel_mandate(state.pool, mandate_id)

    @server.tool()
    async def get_receipt(mandate_id: str) -> dict:
        """The signed intent receipt (events, seal hash) and its verification, so you can keep your own copy."""
        _agent()
        return await m.receipt_with_verification(state.pool, state.pp, state.settings, mandate_id, live=False)

    @server.resource("countersign://policy")
    def policy() -> str:
        return POLICY

    return server


class MCPEndpoint:
    """ASGI endpoint for /mcp: checks the agent key, then hands the request to the MCP session manager."""

    def __init__(self, server: MCPServer, settings):
        self.server, self.settings = server, settings

    async def __call__(self, scope, receive, send):
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        agent = auth.agent_for_key(self.settings, headers.get("authorization"))
        if not agent:
            await JSONResponse({"detail": "agent key required"}, status_code=401)(scope, receive, send)
            return
        token = current_agent.set(agent)
        try:
            await self.server.session_manager.handle_request(scope, receive, send)
        finally:
            current_agent.reset(token)
