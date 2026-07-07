from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import create_pool
from app.routers import campaigns, slack, unsub, webhooks


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not hasattr(app.state, "settings"):  # tests inject state directly
            app.state.settings = get_settings()
            app.state.pool = await create_pool(app.state.settings.database_url)
            yield
            await app.state.pool.close()
        else:
            yield

    app = FastAPI(title="growthable-email", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")
    app.include_router(campaigns.router)
    app.include_router(webhooks.router)
    app.include_router(unsub.router)
    app.include_router(slack.router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def root():
        return {"service": "growthable-email", "docs": "/docs", "health": "/healthz"}

    return app


app = create_app()
