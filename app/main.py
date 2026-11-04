from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db as dbmod
from app.config import Settings, load_settings
from app.mandates import MandateError
from app.paypal import PayPalClient, PayPalError
from app.receipts import load_key


@dataclass
class AppState:
    settings: Settings
    pool: Any
    pp: PayPalClient
    key: Ed25519PrivateKey
    checkpointer: Any = None


def create_app(settings: Settings, *, pool=None, paypal=None) -> FastAPI:
    state = AppState(
        settings=settings,
        pool=pool,
        pp=paypal or PayPalClient(settings.paypal_base_url, settings.paypal_client_id, settings.paypal_client_secret),
        key=load_key(settings.merchant_signing_key),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = state.pool is None
        if owned:
            state.pool = await dbmod.open_pool(settings.database_url)
            await dbmod.apply_schema(state.pool)
        yield
        if owned:
            await state.pool.close()

    app = FastAPI(title="Countersign API", lifespan=lifespan)
    app.state.cs = state
    app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(MandateError)
    async def mandate_error(_: Request, exc: MandateError):
        return JSONResponse({"detail": exc.detail, "violations": exc.violations}, status_code=exc.status)

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
        return {"ok": True, "db": db_ok, "paypal_token_ttl": state.pp.token_ttl()}

    from app.api import router

    app.include_router(router)
    return app


def build() -> FastAPI:
    """uvicorn entry point: `uvicorn app.main:build --factory`."""
    return create_app(load_settings())
