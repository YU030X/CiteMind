import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from alembic.util import CommandError
from evidencehub.config import Settings, validate_database_url
from evidencehub.models import metadata
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

MIGRATION_DATABASE_URL_ENV = "CITEMIND_MIGRATION_DATABASE_URL"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 共享声明式 metadata；迁移仍手写，这里只让 Alembic 能看到模型的约束命名。
target_metadata = metadata


def get_configured_database_url() -> str | None:
    return config.get_main_option("sqlalchemy.url")


def get_offline_database_url() -> str:
    """离线 --sql 只生成 SQL，允许使用应用默认开发 URL。"""

    return get_configured_database_url() or Settings().database_url


def get_online_database_url() -> str:
    """在线迁移必须显式提供高权限 DSN，不回退到应用运行配置。"""

    configured_url = get_configured_database_url()
    if configured_url:
        return configured_url

    migration_url = os.getenv(MIGRATION_DATABASE_URL_ENV)
    if not migration_url:
        raise CommandError(
            f"在线迁移必须设置 {MIGRATION_DATABASE_URL_ENV}（高权限迁移 DSN）；"
            "在线模式不会回退到 CITEMIND_DATABASE_URL 或开发默认 URL。"
        )

    try:
        validate_database_url(migration_url)
    except ValueError as error:
        raise CommandError(
            f"{MIGRATION_DATABASE_URL_ENV} 不符合数据库 URL 规则：{error}"
        ) from error
    return migration_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_offline_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def configure_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_online_database_url()
    engine = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    if sys.platform == "win32":
        # psycopg 异步模式不支持 Windows 默认的 ProactorEventLoop。
        asyncio.run(run_async_migrations(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
