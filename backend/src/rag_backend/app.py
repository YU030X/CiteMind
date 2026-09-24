import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from rag_backend.api.auth import router as auth_router
from rag_backend.api.documents import router as documents_router
from rag_backend.api.errors import (
    REQUEST_ID_HEADER,
    register_exception_handlers,
    sanitize_request_id,
)
from rag_backend.api.knowledge_bases import router as knowledge_bases_router
from rag_backend.auth.passwords import warm_password_hashing
from rag_backend.auth.ratelimit import LoginRateLimiter, create_login_rate_limiter
from rag_backend.config import Settings, get_settings
from rag_backend.database import create_database_engine, create_session_factory


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时预热占位密码哈希，避免首个未知用户登录多出一次 Argon2 计算（侧信道）。
        warm_password_hashing()
        engine = create_database_engine(resolved_settings)
        # 未配置 Redis 时保持 None：登录端据此返回不可用，而不是内存回退放行。
        rate_limiter: LoginRateLimiter | None = create_login_rate_limiter(resolved_settings)
        try:
            app.state.database_engine = engine
            app.state.database_session_factory = create_session_factory(engine)
            app.state.login_rate_limiter = rate_limiter
            yield
        finally:
            if rate_limiter is not None:
                await rate_limiter.close()
            await engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    # 未运行 lifespan（例如 ASGITransport 单测）时的显式默认值。
    app.state.login_rate_limiter = None

    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(knowledge_bases_router)
    app.include_router(documents_router)

    @app.middleware("http")
    async def attach_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        if request_id is None:
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request) -> HealthResponse:
        current_settings: Settings = request.app.state.settings
        return HealthResponse(
            status="ok",
            service="api",
            environment=current_settings.environment,
        )

    return app
