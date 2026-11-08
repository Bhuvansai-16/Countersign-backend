import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app import db as dbmod
from app import loops
from app.config import Settings, load_settings
from app.issues import IssueError
from app.mandates import MandateError
from app.mcp_server import MCPEndpoint, build_mcp
from app.paypal import PayPalClient, PayPalError
from app.receipts import load_key
from app.shopping import Providers


@dataclass
class AppState:
    settings: Settings
    pool: Any
    pp: PayPalClient
    key: Ed25519PrivateKey
    checkpointer: Any = None
    providers: Any = None


def create_app(settings: Settings, *, pool=None, paypal=None) -> FastAPI:
    state = AppState(
        settings=settings,
        pool=pool,
        pp=paypal or PayPalClient(settings.paypal_base_url, settings.paypal_client_id, settings.paypal_client_secret),
        key=load_key(settings.merchant_signing_key),
        providers=Providers(settings),
    )

    mcp_server = build_mcp(state)
    # Agent-key auth happens in MCPEndpoint, so the SDK's localhost-only Host check is not needed.
    mcp_server.streamable_http_app(stateless_http=True, json_response=True,
                                   transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            if state.pool is None:
                state.pool = await dbmod.open_pool(settings.database_url)
                stack.push_async_callback(state.pool.close)
                await dbmod.apply_schema(state.pool)
            if state.checkpointer is None:
                state.checkpointer = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(settings.database_url))
                await state.checkpointer.setup()
            await stack.enter_async_context(mcp_server.session_manager.run())
            loop_task = asyncio.create_task(loops.run_forever(state))
            stack.callback(loop_task.cancel)
            yield

    app = FastAPI(title="Countersign API", lifespan=lifespan)
    app.state.cs = state
    app.state.mcp = mcp_server
    app.router.routes.append(Route("/mcp", endpoint=MCPEndpoint(mcp_server, settings), methods=["GET", "POST", "DELETE"]))
    app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(MandateError)
    async def mandate_error(_: Request, exc: MandateError):
        return JSONResponse({"detail": exc.detail, "violations": exc.violations}, status_code=exc.status)

    @app.exception_handler(IssueError)
    async def issue_error(_: Request, exc: IssueError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(PayPalError)
    async def paypal_error(_: Request, exc: PayPalError):
        status = exc.status if 400 <= exc.status < 500 else 502
        return JSONResponse({"detail": exc.name or "PAYPAL_ERROR", "paypal": exc.body}, status_code=status)

    @app.get("/healthz")
    async def healthz():
        db_ok = False
        if state.pool is not None:
            try:
                db_ok = (await dbmod.fetchrow(state.pool, "SELECT 1 AS ok"))["ok"] == 1
            except Exception:
                db_ok = False
        return {"ok": True, "db": db_ok, "paypal_token_ttl": state.pp.token_ttl(), "llm_model": settings.llm_model}

    from app.api import router
    from app.cases_api import router as cases_router
    from app.sim import router as sim_router
    from app.webhooks import router as webhooks_router
    from app.shopping_api import router as shopping_router

    app.include_router(router)
    app.include_router(cases_router)
    app.include_router(webhooks_router)
    app.include_router(sim_router)
    app.include_router(shopping_router)
    return app


def build() -> FastAPI:
    """uvicorn entry point: `uvicorn app.main:build --factory`."""
    return create_app(load_settings())
