"""身份与会话（Phase 1 首片）：密码哈希、会话令牌、CSRF 与登录限流。

本包只负责服务端会话与登录限流；KB 角色与资源级授权由后续切片在同一授权上下文上叠加。
原会话令牌只存在于 Cookie 与请求内，数据库只保存 token/CSRF 的 hash。
"""

from rag_backend.auth.context import AuthContext
from rag_backend.auth.passwords import hash_password, verify_password

__all__ = [
    "AuthContext",
    "hash_password",
    "verify_password",
]
