from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from database_guard import DestructiveTestDatabase
from sqlalchemy import Connection, create_engine, text

pytestmark = pytest.mark.integration

# 只迁移到本 revision，避免后续新增迁移时本测试悄悄覆盖它们的范围。
PGVECTOR_REVISION = "20260921_0001"
EMBEDDING_DIMENSION = 512


def alembic_config(database_url: str) -> Config:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def alembic_revision(connection: Connection) -> str | None:
    """读取 Alembic 版本表；表不存在即 base。"""

    if connection.scalar(text("SELECT to_regclass('alembic_version')")) is None:
        return None

    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision is None:
        return None
    return str(revision)


def vector_extension_installed(connection: Connection) -> bool:
    extension = connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    return extension is not None


def test_pgvector_migration_upgrade_and_downgrade(
    destructive_test_database: DestructiveTestDatabase,
) -> None:
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            # 迁移前的破坏性检查：确认真的连上了那个 _test 库，且它处于干净初始状态。
            current_database = connection.scalar(text("SELECT current_database()"))
            assert current_database == destructive_test_database.database_name
            assert not vector_extension_installed(connection)
            assert alembic_revision(connection) is None

        command.upgrade(config, PGVECTOR_REVISION)

        with engine.connect() as connection:
            assert vector_extension_installed(connection)
            assert alembic_revision(connection) == PGVECTOR_REVISION
            literal = f"[{','.join(['0'] * EMBEDDING_DIMENSION)}]"
            dimensions = connection.scalar(
                text("SELECT vector_dims(CAST(:literal AS vector))"),
                {"literal": literal},
            )
            assert dimensions == EMBEDDING_DIMENSION

        command.downgrade(config, "base")

        with engine.connect() as connection:
            assert not vector_extension_installed(connection)
            assert alembic_revision(connection) is None
    finally:
        engine.dispose()
