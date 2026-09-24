"""请求级授权上下文。

每次请求都从数据库会话重新构造；不在进程内缓存用户、会话或权限，撤权与禁用立即生效。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    """已通过服务端会话校验的当前用户上下文。"""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    username: str
    is_admin: bool
    session_id: uuid.UUID
    csrf_token: str
