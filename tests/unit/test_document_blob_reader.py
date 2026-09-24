"""只读校验式读取 blob 的聚焦单测：路径绑定、文件类型、上限与内容校验。

这些测试只使用 ``tmp_path``，不连接数据库；真实容器读写与只读挂载由独立 tester 另行验收。
"""

from __future__ import annotations

import os
import subprocess
import traceback
import types
import uuid
from pathlib import Path

import pytest
from rag_backend.ingestion import storage as storage_module
from rag_backend.ingestion.errors import (
    BlobCorrupt,
    BlobNotFound,
    BlobReadError,
    BlobTooLarge,
    BlobUnsafe,
)
from rag_backend.ingestion.storage import (
    DocumentBlobStore,
    InvalidBlobReference,
    content_hash,
)
from rag_backend.ingestion.validation import MAX_MARKDOWN_BYTES


def _write_blob(store: DocumentBlobStore, kb_id: uuid.UUID, content: bytes) -> tuple[str, str]:
    """按内容寻址写入 blob，返回 (file_ref, file_hash)。"""

    file_hash = content_hash(content)
    file_ref = store.blob_ref(kb_id, file_hash)
    target = store.path_for(file_ref)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return file_ref, file_hash


def _symlink_or_skip(link: Path, target: Path) -> None:
    """创建符号链接；平台不允许时跳过对应用例。"""

    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:  # pragma: no cover - 平台相关
        pytest.skip(f"当前环境无法创建符号链接: {error}")


def test_reader_returns_validated_text_for_matching_ref(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    file_ref, file_hash = _write_blob(store, kb_id, "# 标题\n\n正文".encode())

    text = store.read_verified_markdown(kb_id, file_ref, file_hash)

    assert text == "# 标题\n\n正文"


def test_reader_rejects_ref_from_another_kb_or_hash(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    other_kb = uuid.uuid4()
    file_ref, file_hash = _write_blob(store, kb_id, b"# doc")

    # 跨 KB：file_ref 属于另一 KB。
    cross_kb_ref = store.blob_ref(other_kb, file_hash)
    with pytest.raises(InvalidBlobReference):
        store.read_verified_markdown(kb_id, cross_kb_ref, file_hash)
    # 跨摘要：file_ref 的摘要段与传入 file_hash 不一致。
    with pytest.raises(InvalidBlobReference):
        store.read_verified_markdown(kb_id, file_ref, content_hash(b"other"))
    # 非法摘要格式在派生阶段即被拒绝，不触碰文件系统。
    with pytest.raises(InvalidBlobReference):
        store.read_verified_markdown(kb_id, "not-a-ref", file_hash)


def test_reader_rejects_invalid_ref_before_touching_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_ref 不匹配时必须在任何文件系统调用之前拒绝。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    file_hash = content_hash(b"# x")

    def touched(*args: object, **kwargs: object) -> object:
        raise AssertionError("非法引用不得触碰文件系统")

    monkeypatch.setattr(os, "lstat", touched)
    monkeypatch.setattr(os, "open", touched)
    monkeypatch.setattr(os.path, "islink", touched)

    with pytest.raises(InvalidBlobReference):
        store.read_verified_markdown(kb_id, "garbage", file_hash)


def test_reader_rejects_leaf_symlink(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    real = tmp_path / "outside.txt"
    real.write_bytes(b"# outside")
    file_hash = content_hash(real.read_bytes())
    target = store.path_for(store.blob_ref(kb_id, file_hash))
    target.parent.mkdir(parents=True, exist_ok=True)
    _symlink_or_skip(target, real)

    with pytest.raises(BlobUnsafe):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


def test_reader_rejects_leaf_symlink_without_o_nofollow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """模拟缺少 O_NOFOLLOW 的 Windows 分支：仍须在打开前以 lstat 拒绝叶符号链接。"""

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    real = tmp_path / "outside.txt"
    real.write_bytes(b"# outside")
    file_hash = content_hash(real.read_bytes())
    target = store.path_for(store.blob_ref(kb_id, file_hash))
    target.parent.mkdir(parents=True, exist_ok=True)
    _symlink_or_skip(target, real)

    with pytest.raises(BlobUnsafe):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


def test_reader_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# parent"
    file_hash = content_hash(content)
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / file_hash).write_bytes(content)
    kb_dir = tmp_path / str(kb_id)
    _symlink_or_skip(kb_dir, real_dir)

    with pytest.raises(BlobUnsafe):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


def test_reader_rejects_parent_junction_reparse_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows 目录联接点不是 symlink；父目录检测必须同时覆盖 reparse point。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# junction"
    file_hash = content_hash(content)
    kb_dir = tmp_path / str(kb_id)
    kb_dir.mkdir(parents=True)
    (kb_dir / file_hash).write_bytes(content)

    monkeypatch.setattr(
        os.path, "isjunction", lambda path: Path(path) == kb_dir, raising=False
    )

    with pytest.raises(BlobUnsafe):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


@pytest.mark.skipif(os.name != "nt", reason="目录联接点仅 Windows 提供")
def test_reader_rejects_real_windows_junction_parent(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# real junction"
    file_hash = content_hash(content)
    real_dir = tmp_path / "elsewhere-junction"
    real_dir.mkdir()
    (real_dir / file_hash).write_bytes(content)
    kb_dir = tmp_path / str(kb_id)
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(kb_dir), str(real_dir)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:  # pragma: no cover - 环境不允许创建联接点
        pytest.skip(f"无法创建目录联接点: {completed.stderr.strip()}")
    assert os.path.isjunction(kb_dir)
    assert not os.path.islink(kb_dir)

    try:
        with pytest.raises(BlobUnsafe):
            store.read_verified_markdown(
                kb_id, store.blob_ref(kb_id, file_hash), file_hash
            )
    finally:
        # 只移除联接点本身，不进入真实目标目录，避免清理时误删目标内容。
        os.rmdir(kb_dir)


def test_reader_rejects_directory_at_blob_path(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# dir"
    file_hash = content_hash(content)
    target = store.path_for(store.blob_ref(kb_id, file_hash))
    target.mkdir(parents=True)

    with pytest.raises(BlobUnsafe):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


def test_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:
        pytest.skip("仅 POSIX 提供 mkfifo")
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# fifo"
    file_hash = content_hash(content)
    target = store.path_for(store.blob_ref(kb_id, file_hash))
    target.parent.mkdir(parents=True, exist_ok=True)
    mkfifo(target)

    with pytest.raises(BlobUnsafe):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


def test_reader_rejects_missing_blob(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    file_hash = content_hash(b"# gone")

    with pytest.raises(BlobNotFound):
        store.read_verified_markdown(kb_id, store.blob_ref(kb_id, file_hash), file_hash)


def test_reader_error_traceback_omits_storage_root(tmp_path: Path) -> None:
    """底层 OSError 携带绝对 blob 路径；格式化回溯不得暴露存储根。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    file_hash = content_hash(b"# missing")
    file_ref = store.blob_ref(kb_id, file_hash)

    with pytest.raises(BlobNotFound) as excinfo:
        store.read_verified_markdown(kb_id, file_ref, file_hash)

    error = excinfo.value
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert str(store.path_for(file_ref)) not in rendered
    assert str(tmp_path) not in rendered
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_reader_rejects_content_hash_mismatch(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    claimed_content = b"# claimed"
    file_ref, file_hash = _write_blob(store, kb_id, claimed_content)
    # 目标文件被替换成不同内容，但引用与登记摘要仍是原值。
    store.path_for(file_ref).write_bytes(b"# tampered")

    with pytest.raises(BlobCorrupt):
        store.read_verified_markdown(kb_id, file_ref, file_hash)


def test_reader_accepts_exact_byte_limit(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"a" * MAX_MARKDOWN_BYTES
    file_ref, file_hash = _write_blob(store, kb_id, content)

    text = store.read_verified_markdown(kb_id, file_ref, file_hash)

    assert len(text) == MAX_MARKDOWN_BYTES


def test_reader_rejects_file_over_byte_limit(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"a" * (MAX_MARKDOWN_BYTES + 1)
    file_ref, file_hash = _write_blob(store, kb_id, content)

    with pytest.raises(BlobTooLarge):
        store.read_verified_markdown(kb_id, file_ref, file_hash)


def test_reader_rejects_oversize_during_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fstat 报告小尺寸但实际字节超限时，读取循环仍必须失败。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"a" * (MAX_MARKDOWN_BYTES + 1)
    file_ref, file_hash = _write_blob(store, kb_id, content)

    real_fstat = os.fstat

    def fake_fstat(fd: int) -> object:
        info = real_fstat(fd)
        return types.SimpleNamespace(st_mode=info.st_mode, st_size=0)

    monkeypatch.setattr(os, "fstat", fake_fstat)

    with pytest.raises(BlobTooLarge):
        store.read_verified_markdown(kb_id, file_ref, file_hash)


def test_reader_rejects_empty_and_corrupt_utf8(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()

    empty_ref, empty_hash = _write_blob(store, kb_id, b"")
    with pytest.raises(BlobCorrupt):
        store.read_verified_markdown(kb_id, empty_ref, empty_hash)

    invalid_ref, invalid_hash = _write_blob(store, kb_id, b"\xff\xfe not utf8")
    with pytest.raises(BlobCorrupt):
        store.read_verified_markdown(kb_id, invalid_ref, invalid_hash)

    control_ref, control_hash = _write_blob(store, kb_id, b"# ok\x00binary")
    with pytest.raises(BlobCorrupt):
        store.read_verified_markdown(kb_id, control_ref, control_hash)


def test_reader_wraps_mid_read_oserror_and_closes_fd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """读取期 OSError 归一为 BlobReadError 子类，且描述符仍被关闭。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    file_ref, file_hash = _write_blob(store, kb_id, b"# ok")

    closed: list[int] = []
    real_close = os.close

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def failing_read(fd: int, count: int) -> bytes:
        raise OSError("simulated read failure")

    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(os, "read", failing_read)

    with pytest.raises(BlobReadError):
        store.read_verified_markdown(kb_id, file_ref, file_hash)
    assert len(closed) == 1


def test_reader_closes_descriptor_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    file_ref, file_hash = _write_blob(store, kb_id, b"# ok")

    closed: list[int] = []
    real_close = os.close

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", spy_close)

    assert store.read_verified_markdown(kb_id, file_ref, file_hash) == "# ok"
    assert len(closed) == 1

    # 内容被篡改导致摘要校验失败时，文件描述符仍应在 finally 中关闭。
    store.path_for(file_ref).write_bytes(b"# tampered")
    with pytest.raises(BlobCorrupt):
        store.read_verified_markdown(kb_id, file_ref, file_hash)
    assert len(closed) == 2

    # 超过字节上限时同样必须关闭描述符。
    over_content = b"a" * (MAX_MARKDOWN_BYTES + 1)
    over_ref, over_hash = _write_blob(store, kb_id, over_content)
    with pytest.raises(BlobTooLarge):
        store.read_verified_markdown(kb_id, over_ref, over_hash)
    assert len(closed) == 3


def test_reader_module_reexports_chunk_constant() -> None:
    """读取块大小是模块级实现细节，保持可检视以约束有界读取。"""

    assert 0 < storage_module._READ_CHUNK_BYTES <= MAX_MARKDOWN_BYTES
