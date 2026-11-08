"""Countersign MCP server for buyer agents: quote and mandate, never pay."""

import json
from contextvars import ContextVar

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from app import auth
from app import db as dbmod
from app import issues
from app import mandates as m
from app import shopping
from fastapi import HTTPException

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

    async def _own(mandate_id: str) -> None:
        """Agents only see their own mandates; anything else looks like it does not exist."""
        row = await dbmod.fetchrow(state.pool, "SELECT agent_id FROM receipts WHERE id = %s", mandate_id)
        if not row or row["agent_id"] != _agent():
            raise LookupError("mandate not found")

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
        await _own(mandate_id)
        return await m.mandate_status(state.pool, mandate_id)

    @server.tool()
    async def cancel_mandate(mandate_id: str) -> dict:
        """Cancel a mandate that the human has not confirmed yet."""
        await _own(mandate_id)
        return await m.cancel_mandate(state.pool, mandate_id)

    @server.tool()
    async def get_receipt(mandate_id: str) -> dict:
        """The signed intent receipt (events, seal hash) and its verification, so you can keep your own copy."""
        await _own(mandate_id)
        return await m.receipt_with_verification(state.pool, state.pp, state.settings, mandate_id, live=False)

    async def _issue_call(coro) -> dict:
        try:
            return await coro
        except issues.IssueError as e:
            return {"error": "REJECTED", "status": e.status, "detail": e.detail}

    @server.tool()
    async def report_issue(mandate_id: str, issue_type: str, description: str, desired_resolution: str) -> dict:
        """Report a problem with a paid order before opening a PayPal dispute. issue_type: WRONG_ITEM, DAMAGED,
        NOT_RECEIVED, NOT_AS_DESCRIBED or OTHER. desired_resolution: REFUND, PARTIAL_REFUND, REPLACEMENT or RETURN.
        The seller reviews it and may send an offer; poll get_issue_status."""
        return await _issue_call(issues.report_issue(state, mandate_id, _agent(), issue_type, description, desired_resolution))

    @server.tool()
    async def respond_to_proposal(issue_id: str, decision: str, counter_amount: str | None = None) -> dict:
        """Answer the seller's offer: ACCEPT (a refund is paid at once), DECLINE, or COUNTER once with counter_amount
        (a USD amount up to what was paid). Only call this after get_issue_status shows state OFFERED."""
        return await _issue_call(issues.respond_to_proposal(state, issue_id, _agent(), decision, counter_amount))

    @server.tool()
    async def get_issue_status(issue_id: str) -> dict:
        """Status of an issue you reported: PROPOSED (seller reviewing), OFFERED (answer it), RESOLVED or REJECTED."""
        return await _issue_call(issues.get_issue_status(state, issue_id, _agent()))

    if state.settings.demo_enabled and state.settings.paypal_base_url == "https://api-m.sandbox.paypal.com":
        async def shopping_call(coro):
            try:
                return await coro
            except (HTTPException, m.MandateError) as exc:
                return {"error": exc.detail, "violations": getattr(exc, "violations", [])}

        @server.tool()
        async def search_external_products(query: str, max_amount: str) -> dict:
            """Search Channel3 for real USD products within a total budget including 6 USD sandbox shipping.
            These are discovery candidates, not products our merchant fulfills. Returns candidate_id values."""
            return await shopping_call(shopping.search(state, _agent(), query, max_amount))

        @server.tool()
        async def inspect_external_product(candidate_id: str) -> dict:
            """Read a discovered retailer listing in Kernel. Returns a live view and a supplementary observation;
            this does not verify retailer facts, make a purchase, or change the merchant catalog."""
            async def run():
                browser = await shopping.start_browser(state, _agent(), candidate_id)
                observation = await shopping.inspect(state, _agent(), candidate_id)
                return {"candidate_id": candidate_id, **browser, "observation": observation,
                        "retailer_facts_verified": False}
            return await shopping_call(run())

        @server.tool()
        async def stop_external_browser(candidate_id: str) -> dict:
            """Stop the Kernel browser for this candidate after inspection to stop session usage."""
            return await shopping_call(shopping.stop_browser(state, _agent(), candidate_id))

        @server.tool()
        async def create_external_mandate(candidate_id: str, instruction: str, max_amount: str,
                                          attributes: dict | None = None) -> dict:
            """Create a clearly labelled SANDBOX COPY at Maya's Loom using a Channel3 discovery snapshot.
            This never buys from the source retailer. Give the confirm_url to the human; never confirm or pay."""
            return await shopping_call(shopping.prepare(state, _agent(), candidate_id, instruction, max_amount, attributes or {}))

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
