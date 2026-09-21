from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel

from evidencehub.config import Settings, get_settings
from evidencehub.database import create_database_engine, create_session_factory


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(resolved_settings)
        try:
            app.state.database_engine = engine
            app.state.database_session_factory = create_session_factory(engine)
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request) -> HealthResponse:
        current_settings: Settings = request.app.state.settings
        return HealthResponse(
            status="ok",
            service="api",
            environment=current_settings.environment,
        )

    return app
