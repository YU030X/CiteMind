"""共享的声明式基类、约束命名规则与时间戳混入。

Alembic 的 ``target_metadata`` 指向这里的 ``metadata``，让模型与迁移使用同一套
约束命名；迁移仍然手写，本文件不生成迁移，也不包含任何投机抽象。
"""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束名格式固定；迁移里写出的完整约束名必须与这些规则一致。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


class CreatedAtMixin:
    """UTC 创建时间；由数据库 ``now()`` 提供默认值。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UpdatedAtMixin:
    """UTC 更新时间；数据库提供默认值，ORM 更新时同步刷新。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
