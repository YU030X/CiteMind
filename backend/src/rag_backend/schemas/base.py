"""所有外部 schema 的共享基类：字段用 camelCase 对外，Python 侧保持 snake_case。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """外部 JSON 字段一律 camelCase；同时允许按 Python 字段名构造。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StrictCamelModel(CamelModel):
    """在 camelCase 别名之上禁止未知字段：客户端不能悄悄提交服务端不认识的参数。"""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


__all__ = ["CamelModel", "StrictCamelModel"]
