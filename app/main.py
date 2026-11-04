from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db as dbmod
from app.config import Settings, load_settings


def create_app(settings: Settings, *, pool=None, paypal=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = app.state.pool is None
        if owned:
            app.state.pool = await dbmod.open_pool(settings.database_url)
            await dbmod.apply_schema(app.state.pool)
        yield
        if owned:
            await app.state.pool.close()

    app = FastAPI(title="Countersign API", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = pool

    @app.get("/healthz")
    async def healthz():
        db_ok = False
        if app.state.pool is not None:
            try:
                db_ok = (await dbmod.fetchrow(app.state.pool, "SELECT 1 AS ok"))["ok"] == 1
            except Exception:
                db_ok = False
        return {"ok": True, "db": db_ok}

    return app


def build() -> FastAPI:
    """uvicorn entry point: `uvicorn app.main:build --factory`."""
    return create_app(load_settings())
