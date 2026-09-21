from typing import Literal, cast

import evidencehub.app as app_module
import pytest
from evidencehub.app import create_app
from evidencehub.config import DEFAULT_DATABASE_URL, Settings
from evidencehub.database import create_database_engine, create_session_factory
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

PRODUCTION_DATABASE_URL = "postgresql+psycopg://citemind_app:strong-password@postgres:5432/citemind"


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def test_settings_rejects_non_postgresql_driver() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(database_url="sqlite+aiosqlite:///./citemind.db")


@pytest.mark.parametrize(
    ("database_url", "error_message"),
    [
        ("postgresql+psycopg:///citemind", "非空 host"),
        ("postgresql+psycopg://citemind:citemind@localhost:5432/", "非空 database"),
    ],
    ids=["missing-host", "missing-database"],
)
def test_settings_rejects_missing_database_host_or_name(
    database_url: str, error_message: str
) -> None:
    with pytest.raises(ValidationError, match=error_message):
        Settings(database_url=database_url)


@pytest.mark.parametrize(
    "database_url",
    [
        DEFAULT_DATABASE_URL,
        "postgresql+psycopg://citemind:citemind@postgres:5432/production",
        "postgresql+psycopg://admin:citemind@postgres:5432/citemind",
    ],
    ids=["default-credentials", "default-credentials-on-host", "default-password-only"],
)
def test_settings_rejects_default_credentials_in_production(database_url: str) -> None:
    with pytest.raises(ValidationError, match="默认数据库凭据"):
        Settings(environment="production", database_url=database_url)


def test_settings_requires_username_in_production() -> None:
    with pytest.raises(ValidationError, match="非空 username"):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://:secret@postgres:5432/citemind",
        )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://citemind@postgres:5432/citemind",
        "postgresql+psycopg://citemind:@postgres:5432/citemind",
    ],
    ids=["missing-password", "empty-password"],
)
def test_settings_requires_password_in_production(database_url: str) -> None:
    with pytest.raises(ValidationError, match="非空 password"):
        Settings(environment="production", database_url=database_url)


def test_settings_rejects_database_echo_in_production() -> None:
    with pytest.raises(ValidationError, match="database_echo"):
        Settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            database_echo=True,
        )


def test_settings_accepts_production_database_configuration() -> None:
    settings = Settings(environment="production", database_url=PRODUCTION_DATABASE_URL)

    assert settings.database_echo is False


@pytest.mark.parametrize("environment", ["development", "test"])
def test_settings_keeps_development_and_test_usable(
    environment: Literal["development", "test"],
) -> None:
    settings = Settings(environment=environment, database_echo=True)

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.database_echo is True


@pytest.mark.parametrize("environment", ["development", "test"])
def test_settings_allows_default_credentials_outside_production(
    environment: Literal["development", "test"],
) -> None:
    settings = Settings(environment=environment, database_url=DEFAULT_DATABASE_URL)

    assert settings.environment == environment


@pytest.mark.anyio
async def test_database_engine_and_session_factory_can_be_created_and_disposed() -> None:
    settings = Settings(environment="test")
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    assert isinstance(engine, AsyncEngine)
    async with session_factory() as session:
        assert session.is_active

    await engine.dispose()


@pytest.mark.anyio
async def test_fastapi_lifespan_creates_and_disposes_database_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposable_engine = DisposableEngine()
    engine = cast(AsyncEngine, disposable_engine)
    monkeypatch.setattr(app_module, "create_database_engine", lambda settings: engine)

    app = create_app(Settings(environment="test"))
    async with app.router.lifespan_context(app):
        assert app.state.database_engine is engine
        assert app.state.database_session_factory is not None
        assert not disposable_engine.disposed

    assert disposable_engine.disposed
