"""原文件的私有内容寻址存储。

存储根来自服务端配置（Compose 中挂载 api 专用命名卷）。相对路径只由服务端
``knowledge_base.id`` 与内容 SHA-256 派生，绝不拼接用户文件名；写入先落同目录临时
文件、``fsync`` 后 ``os.replace`` 原子发布，失败只清理本次临时文件。最终 blob 是 KB
范围内可复用的内容寻址文件：不同文档、不同 Idempotency-Key 但内容相同的上传共享同一
份 blob。事务冲突时不删除已发布的最终 blob，因为并发其他事务可能已引用它；由此产生的
孤儿文件窗口留待后续 GC 作业处理（本切片不实现 GC）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from rag_backend.ingestion.errors import (
    BlobCorrupt,
    BlobNotFound,
    BlobTooLarge,
    BlobUnsafe,
    DocumentEmpty,
    DocumentNotText,
    IngestionError,
)
from rag_backend.ingestion.validation import MAX_MARKDOWN_BYTES, decode_markdown_content

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
# 单次读取块大小；只影响内存占用与系统调用次数，不影响 20,000,000 字节上限判定。
_READ_CHUNK_BYTES = 64 * 1024


def _is_link_like(path: Path) -> bool:
    """符号链接或 Windows 目录联接点（reparse point）都视为不可信路径。"""

    if os.path.islink(path):
        return True
    # ``os.path.isjunction`` 在 Python 3.12+ 可用；缺失的平台按不可信处理。
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction(path))
    except (OSError, ValueError):
        # 类型无法确认时宁拒绝不放过。
        return True


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

    def read_verified_markdown(
        self, kb_id: uuid.UUID, file_ref: str, file_hash: str
    ) -> str:
        """校验式有限读取：只读取由该 KB 与摘要派生的 blob，返回已校验文本。

        调用方必须同时传入数据库登记的 ``kb_id``、``file_ref`` 与 ``file_hash``。本方法
        先要求 ``file_ref`` 严格等于服务端由二者派生的路径（含摘要格式校验），再以有界
        方式读取，最后核对内容摘要并复用上传侧同一套 UTF-8/控制字节校验。除非法
        ``file_ref`` 抛 ``InvalidBlobReference``（调用方需单独映射）外，缺失、非常规文件、
        超限、IO 失败与内容损坏都抛出 ``BlobReadError`` 子类，使未来 worker 能单 catch
        处理所有读失败；错误信息静态，且底层携带路径的 ``OSError`` 用 ``from None`` 抑制。
        本方法不写数据库，也不决定 job 状态。

        符号链接与联接点防护：只要平台提供 ``os.O_NOFOLLOW`` 就用于打开，随后对同一个
        已打开文件描述符 ``fstat``；所有平台在打开前还会 ``lstat`` 叶节点并逐段检查存储
        根到目标之间的父目录是否为符号链接或 Windows 目录联接点（reparse point，不是
        ``islink``）。但父目录检查与打开之间仍存在 TOCTOU 窗口，纯标准库无法在 Windows
        上完全消除；该窗口只能通过独占文件系统权限或 ``openat``/句柄相对打开进一步收紧。
        无论成功、超限还是校验失败，文件描述符都在 ``finally`` 中关闭。
        """

        # 先做纯字符串级校验：blob_ref 会拒绝非法摘要，file_ref 必须与之完全一致，
        # 从而拒绝跨 KB、跨摘要或用户构造的任意路径。
        if file_ref != self.blob_ref(kb_id, file_hash):
            raise InvalidBlobReference("file_ref 与 kb_id/内容摘要不匹配")
        target = self.path_for(file_ref)
        data = self._read_bounded_regular_file(target)
        if content_hash(data) != file_hash:
            raise BlobCorrupt("blob 内容摘要与登记值不一致")
        try:
            return decode_markdown_content(data)
        except (DocumentEmpty, DocumentNotText):
            # 空内容、非法 UTF-8 或二进制控制字节统一按内容损坏处理。
            raise BlobCorrupt("blob 内容不是有效的 Markdown 文本") from None

    def _read_bounded_regular_file(self, target: Path) -> bytes:
        """以有界读取返回字节；拒绝符号链接/联接点、非常规文件、缺失与超限。"""

        self._reject_symlinked_parents(target)
        descriptor = self._open_no_follow(target)
        try:
            try:
                info = os.fstat(descriptor)
            except OSError:
                raise BlobUnsafe("无法读取 blob 文件状态") from None
            if not stat.S_ISREG(info.st_mode):
                raise BlobUnsafe("blob 不是普通文件")
            if info.st_size > MAX_MARKDOWN_BYTES:
                raise BlobTooLarge("blob 超过单文件字节上限")
            try:
                return self._read_bytes(descriptor)
            except OSError:
                raise BlobUnsafe("读取 blob 文件失败") from None
        finally:
            # 只读描述符关闭失败不改变已获得的结果，也不得掩盖前面的领域错误。
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _reject_symlinked_parents(self, target: Path) -> None:
        """逐段拒绝到目标的符号链接/联接点目录；无法完全消除父目录 TOCTOU。"""

        try:
            parts = target.relative_to(self._root).parts[:-1]
        except ValueError:
            raise BlobUnsafe("blob 路径不在存储根内") from None
        current = self._root
        for part in parts:
            current = current / part
            if _is_link_like(current):
                raise BlobUnsafe("blob 所在目录是符号链接或联接点")

    @staticmethod
    def _open_no_follow(target: Path) -> int:
        """打开目标文件；可用时带 ``O_NOFOLLOW``，缺失时返回可读文件描述符。"""

        # Windows 没有 O_NOFOLLOW：先 lstat 叶节点，符号链接/联接点在此拒绝；Linux 上
        # O_NOFOLLOW 让内核在打开的同一时刻拒绝叶符号链接。
        try:
            leaf = os.lstat(target)
        except FileNotFoundError:
            raise BlobNotFound("blob 文件不存在") from None
        except OSError:
            raise BlobUnsafe("无法读取 blob 文件状态") from None
        if stat.S_ISLNK(leaf.st_mode) or _is_link_like(target):
            raise BlobUnsafe("blob 文件是符号链接或联接点")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        # FIFO 上带 O_NONBLOCK 的只读打开不会阻塞；普通文件不受影响。
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        try:
            return os.open(target, flags)
        except FileNotFoundError:
            raise BlobNotFound("blob 文件不存在") from None
        except OSError:
            raise BlobUnsafe("无法以只读方式打开 blob 文件") from None

    @staticmethod
    def _read_bytes(descriptor: int) -> bytes:
        """从已校验的文件描述符有界读取；读取中超出上限时立即失败。"""

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor, min(_READ_CHUNK_BYTES, MAX_MARKDOWN_BYTES - total + 1)
            )
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_MARKDOWN_BYTES:
                raise BlobTooLarge("blob 超过单文件字节上限")
            chunks.append(chunk)

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
