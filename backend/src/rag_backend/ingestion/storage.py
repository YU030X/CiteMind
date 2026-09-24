"""原文件的私有内容寻址存储。

存储根来自服务端配置（Compose 中挂载 api 专用命名卷）。相对路径只由服务端
``knowledge_base.id`` 与内容 SHA-256 派生，绝不拼接用户文件名；写入先落同目录临时
文件、``fsync`` 后 ``os.replace`` 原子发布，失败只清理本次临时文件。最终 blob 是 KB
范围内可复用的内容寻址文件：不同文档、不同 Idempotency-Key 但内容相同的上传共享同一
份 blob。事务冲突时不删除已发布的最终 blob，因为并发其他事务可能已引用它；由此产生的
孤儿文件窗口留待后续 GC 作业处理（本切片不实现 GC）。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path

from rag_backend.ingestion.errors import IngestionError

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class InvalidBlobReference(IngestionError):
    """``file_ref`` 不是本模块派生出的安全路径。"""


def content_hash(data: bytes) -> str:
    """返回内容 SHA-256 的十六进制小写摘要。"""

    return hashlib.sha256(data).hexdigest()


class DocumentBlobStore:
    """按 KB 与内容摘要组织原文件的私有存储。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def blob_ref(self, kb_id: uuid.UUID, file_hash: str) -> str:
        """由服务端 KB ID 与内容摘要派生相对引用；不含任何用户输入。"""

        if not _HASH_PATTERN.fullmatch(file_hash):
            raise InvalidBlobReference("内容摘要必须是 64 位十六进制")
        return f"{kb_id}/{file_hash}"

    def path_for(self, file_ref: str) -> Path:
        """把相对引用解析为绝对路径；引用不合规则拒绝，防止路径穿越。"""

        parts = file_ref.split("/")
        if len(parts) != 2:
            raise InvalidBlobReference("file_ref 必须是 <kb_id>/<sha256> 形式")
        kb_part, hash_part = parts
        try:
            kb_id = uuid.UUID(kb_part)
        except ValueError as error:
            raise InvalidBlobReference("file_ref 的 KB 段不是合法 UUID") from error
        if str(kb_id) != kb_part or not _HASH_PATTERN.fullmatch(hash_part):
            raise InvalidBlobReference("file_ref 含非法片段")
        return self._root / kb_part / hash_part

    def exists(self, file_ref: str) -> bool:
        return self.path_for(file_ref).is_file()

    def publish(self, kb_id: uuid.UUID, file_hash: str, content: bytes) -> str:
        """原子发布内容到 KB 作用域 blob；已存在同内容普通文件时复用，不重写。

        返回相对 ``file_ref``。写入失败只删除本次临时文件；已发布的最终 blob 永不被
        本方法删除。Windows 上并发覆盖同一目标时 ``os.replace`` 可能报 WinError 5：
        只有当目标已由别的写入者原子发布、且仍是 SHA-256 与预期吻合的普通文件（非
        符号链接）时才承认复用；目标缺失、内容不匹配或无法确认时原样抛出，绝不吞
        掉泛化的 ``PermissionError``。
        """

        file_ref = self.blob_ref(kb_id, file_hash)
        target = self.path_for(file_ref)
        if self._is_reusable_target(target, file_hash):
            return file_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".upload-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temporary_name, target)
            except OSError:
                # 另一个写入者可能已用同内容原子发布；只有核实后才能承认复用。
                if self._is_reusable_target(target, file_hash):
                    return file_ref
                raise
        finally:
            # 成功发布后临时文件已被移走；失败或复用路径下必须清掉本次临时文件。
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        return file_ref

    @staticmethod
    def _is_reusable_target(target: Path, file_hash: str) -> bool:
        """目标必须是 SHA-256 吻合的普通文件且非符号链接，才允许复用。"""

        try:
            if target.is_symlink() or not target.is_file():
                return False
            return content_hash(target.read_bytes()) == file_hash
        except OSError:
            return False
