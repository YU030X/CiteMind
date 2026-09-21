from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from evidencehub.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_database_engine(settings: Settings) -> AsyncEngine:
    """为一个进程创建共享连接池。"""

    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    """创建每次调用都会产生独立 Session 的工厂。"""

    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为单个 API 请求提供独立 Session，并在请求结束时关闭。"""

    factory: SessionFactory = request.app.state.database_session_factory
    async with factory() as session:
        yield session
