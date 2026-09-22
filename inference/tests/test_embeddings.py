"""本地模型目录校验：缺失或不完整必须在导入 torch 之前失败并使启动中止。"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support import build_settings

from citemind_inference.app import create_app
from citemind_inference.embeddings import (
    EmbeddingModelError,
    inspect_model_directory,
    load_embedder,
)
from citemind_inference.model_identity import (
    MODEL_ARTIFACT_DIGESTS,
    MODEL_ARTIFACT_FILES,
    MODEL_NAME,
    MODEL_REVISION,
    manifest_path,
)


def test_missing_model_directory_raises(tmp_path: Path) -> None:
    settings = build_settings(embedding_model_path=tmp_path / "missing")

    with pytest.raises(EmbeddingModelError) as error_info:
        inspect_model_directory(settings)

    assert "不从网络下载" in str(error_info.value)


def test_incomplete_model_directory_lists_missing_files(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    settings = build_settings(embedding_model_path=tmp_path)

    with pytest.raises(EmbeddingModelError) as error_info:
        inspect_model_directory(settings)

    message = str(error_info.value)
    assert "tokenizer_config.json" in message
    assert "vocab.txt" in message


def test_model_directory_without_weights_is_rejected(tmp_path: Path) -> None:
    for name in ("config.json", "tokenizer_config.json", "vocab.txt"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    settings = build_settings(embedding_model_path=tmp_path)

    with pytest.raises(EmbeddingModelError) as error_info:
        inspect_model_directory(settings)

    assert "缺少权重" in str(error_info.value)


def test_missing_model_never_imports_torch(tmp_path: Path) -> None:
    settings = build_settings(embedding_model_path=tmp_path / "missing")
    before = set(sys.modules)

    with pytest.raises(EmbeddingModelError):
        load_embedder(settings)

    newly_imported = set(sys.modules) - before
    assert not any(name == "torch" or name.startswith("torch.") for name in newly_imported)
    assert "citemind_inference.transformers_embedder" not in newly_imported


def test_startup_fails_when_model_is_missing(tmp_path: Path) -> None:
    settings = build_settings(embedding_model_path=tmp_path / "missing")
    app = create_app(settings, embedder_factory=load_embedder)

    with pytest.raises(EmbeddingModelError):
        with TestClient(app):
            pass


def test_startup_fails_when_model_bytes_are_tampered(tmp_path: Path) -> None:
    # 六个文件名齐备、清单自称正确，但实际字节被替换：必须启动失败而不是加载伪造权重。
    for name in MODEL_ARTIFACT_FILES:
        (tmp_path / name).write_bytes(b"forged")
    manifest_path(tmp_path).write_text(
        json.dumps(
            {
                "model": MODEL_NAME,
                "revision": MODEL_REVISION,
                "files": {
                    name: {"size": size, "sha256": sha256}
                    for name, (size, sha256) in MODEL_ARTIFACT_DIGESTS.items()
                },
            }
        ),
        encoding="utf-8",
    )
    settings = build_settings(embedding_model_path=tmp_path)
    app = create_app(settings, embedder_factory=load_embedder)

    with pytest.raises(EmbeddingModelError):
        with TestClient(app):
            pass
