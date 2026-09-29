"""reranker 产物清单身份校验回归：只验证「拒绝」路径，不加载 torch/transformers。

构建期清单的生成由 ``scripts/prepare_model.py --model rerank`` 负责，其正向验收需要真实下载，
不在本轮声称。这里证明运行期离线核对能拒绝身份漂移、文件集合不符与字节替换。
"""

import hashlib
import json
from pathlib import Path

import pytest

from citemind_inference.rerank_identity import (
    RERANK_MODEL_NAME,
    RERANK_MODEL_REVISION,
    RerankIdentityError,
    manifest_path,
    verify_rerank_artifacts,
)

FILES = ("config.json", "sentencepiece.bpe.model", "model.safetensors")


def write_model(model_dir: Path, *, extra: tuple[str, ...] = ()) -> dict[str, dict[str, object]]:
    model_dir.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, object]] = {}
    for name in FILES:
        payload = f"bytes-of-{name}".encode()
        (model_dir / name).write_bytes(payload)
        records[name] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    for name in extra:
        (model_dir / name).write_bytes(b"extra")
    return records


def write_manifest(
    model_dir: Path,
    records: dict[str, dict[str, object]],
    *,
    model: str = RERANK_MODEL_NAME,
    revision: str = RERANK_MODEL_REVISION,
) -> None:
    """写入清单；model/revision 默认等于冻结身份。"""

    manifest_path(model_dir).write_text(
        json.dumps({"model": model, "revision": revision, "files": records}),
        encoding="utf-8",
    )


def test_valid_manifest_passes(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-reranker-base"
    records = write_model(model_dir)
    write_manifest(model_dir, records)

    verify_rerank_artifacts(model_dir)


def test_tampered_bytes_are_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-reranker-base"
    records = write_model(model_dir)
    write_manifest(model_dir, records)
    original = (model_dir / "model.safetensors").read_bytes()
    # 保持字节数不变，只改内容，验证比对的是摘要而不是大小。
    (model_dir / "model.safetensors").write_bytes(b"x" * len(original))

    with pytest.raises(RerankIdentityError) as error_info:
        verify_rerank_artifacts(model_dir)

    assert "SHA-256 不符" in str(error_info.value)


def test_wrong_revision_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-reranker-base"
    records = write_model(model_dir)
    write_manifest(model_dir, records, revision="0" * 40)

    with pytest.raises(RerankIdentityError) as error_info:
        verify_rerank_artifacts(model_dir)

    assert "身份" in str(error_info.value)


def test_extra_directory_file_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-reranker-base"
    records = write_model(model_dir, extra=("pytorch_model.bin",))
    write_manifest(model_dir, records)

    with pytest.raises(RerankIdentityError) as error_info:
        verify_rerank_artifacts(model_dir)

    assert "额外" in str(error_info.value)


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-reranker-base"
    write_model(model_dir)

    with pytest.raises(RerankIdentityError) as error_info:
        verify_rerank_artifacts(model_dir)

    assert "缺少 reranker 产物清单" in str(error_info.value)
