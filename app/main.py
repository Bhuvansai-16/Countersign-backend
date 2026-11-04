from fastapi import FastAPI

from app.config import Settings


def create_app(settings: Settings, *, pool=None, paypal=None) -> FastAPI:
    app = FastAPI(title="Countersign API")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
