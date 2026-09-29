"""可信模型字节校验回归：目录集合、钉死摘要与产物清单的交叉核对。

这里只验证「拒绝」路径；真实权重的正向验收在 ``test_real_model.py`` 的 opt-in 测试里。
永不加载 torch 或 transformers。
"""

import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from support import build_settings

from citemind_inference.embeddings import (
    EmbeddingModelError,
    inspect_model_directory,
    load_embedder,
)
from citemind_inference.model_identity import (
    MANIFEST_NAME,
    MODEL_ARTIFACT_DIGESTS,
    MODEL_ARTIFACT_FILES,
    MODEL_NAME,
    MODEL_REVISION,
    ModelIdentityError,
    manifest_path,
    verify_model_artifacts,
)

PREPARE_MODEL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_model.py"
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "golden-reference.json"
GOLDEN_SHA256 = "967e700bc3baf8147fcfe8919c2b8a8e665a82f3bb2fc7ff7fcf33850b8eb057"


def write_forged_model_dir(model_dir: Path, *, extra: tuple[str, ...] = ()) -> None:
    """写出 6 个文件名但内容全为伪造字节；用于证明只信字节而不是文件名。"""

    model_dir.mkdir(parents=True, exist_ok=True)
    for name in MODEL_ARTIFACT_FILES:
        (model_dir / name).write_bytes(b"forged")
    for name in extra:
        (model_dir / name).write_bytes(b"forged")


def write_manifest(
    model_dir: Path,
    *,
    model: str = MODEL_NAME,
    revision: str = MODEL_REVISION,
    files: dict[str, Any] | None = None,
) -> None:
    if files is None:
        files = {
            name: {"size": size, "sha256": sha256}
            for name, (size, sha256) in MODEL_ARTIFACT_DIGESTS.items()
        }
    manifest_path(model_dir).write_text(
        json.dumps({"model": model, "revision": revision, "files": files}),
        encoding="utf-8",
    )


def test_manifest_is_not_trusted_over_pinned_bytes(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir)
    # 清单自称是正确的身份与摘要，但实际字节被替换。
    write_manifest(model_dir)

    with pytest.raises(ModelIdentityError) as error_info:
        verify_model_artifacts(model_dir)

    assert "大小不符" in str(error_info.value) or "SHA-256 不符" in str(error_info.value)


def test_wrong_revision_in_manifest_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir)
    write_manifest(model_dir, revision="0" * 40)

    with pytest.raises(ModelIdentityError) as error_info:
        verify_model_artifacts(model_dir)

    assert "身份" in str(error_info.value)
    assert MODEL_REVISION in str(error_info.value)


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir)

    with pytest.raises(ModelIdentityError) as error_info:
        verify_model_artifacts(model_dir)

    assert "缺少模型产物清单" in str(error_info.value)


def test_manifest_file_set_mismatch_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir)
    files = {
        name: {"size": size, "sha256": sha256}
        for name, (size, sha256) in MODEL_ARTIFACT_DIGESTS.items()
        if name != "vocab.txt"
    }
    write_manifest(model_dir, files=files)

    with pytest.raises(ModelIdentityError) as error_info:
        verify_model_artifacts(model_dir)

    assert "文件集合" in str(error_info.value)


def test_extra_file_in_model_directory_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir, extra=("pytorch_model.bin",))
    write_manifest(model_dir)

    with pytest.raises(ModelIdentityError) as error_info:
        verify_model_artifacts(model_dir)

    assert "额外" in str(error_info.value)
    assert "pytorch_model.bin" in str(error_info.value)


def test_inspect_rejects_tampered_bytes_and_never_imports_torch(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir)
    write_manifest(model_dir)
    settings = build_settings(embedding_model_path=model_dir)
    before = set(sys.modules)

    with pytest.raises(EmbeddingModelError):
        load_embedder(settings)

    newly_imported = set(sys.modules) - before
    assert not any(name == "torch" or name.startswith("torch.") for name in newly_imported)
    assert "citemind_inference.transformers_embedder" not in newly_imported


def test_inspect_rejects_extra_weight_file(tmp_path: Path) -> None:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    write_forged_model_dir(model_dir, extra=("pytorch_model.bin",))
    write_manifest(model_dir)
    settings = build_settings(embedding_model_path=model_dir)

    with pytest.raises(EmbeddingModelError):
        inspect_model_directory(settings)


def test_golden_reference_is_audited_and_complete() -> None:
    """独立 golden 参考必须在常驻测试里保持字节与结构完整。"""

    raw = GOLDEN_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == GOLDEN_SHA256
    data = json.loads(raw.decode("utf-8"))
    assert data["model"]["revision"] == MODEL_REVISION
    assert data["reference_method"]["exact_revision"] == MODEL_REVISION
    assert "使用容差比较，尚未实测跨 CPU" in data["reference_method"]["cross_env_note"]
    samples = data["samples"]
    assert len(samples) == 3
    for sample in samples:
        assert len(sample["token_ids"]) == sample["token_count"]
        assert len(sample["vector"]) == 512
        assert sample["finite"] is True


# ---------------------------------------------------------------- 防漂移


def script_assignment(name: str) -> Any:
    """从构建期脚本里取出字面量赋值；脚本产物不能只靠同名常量保持一致。"""

    tree = ast.parse(PREPARE_MODEL_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                assert node.value is not None
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            targets = [target for target in node.targets if isinstance(target, ast.Name)]
            if any(target.id == name for target in targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"构建期脚本缺少字面量 {name}")


def test_prepare_model_constants_match_package_identity() -> None:
    assert script_assignment("MODEL") == MODEL_NAME
    assert script_assignment("REVISION") == MODEL_REVISION
    assert script_assignment("MANIFEST") == MANIFEST_NAME
    script_files = script_assignment("EXPECTED_FILES")
    assert tuple(script_files) == MODEL_ARTIFACT_FILES
    for name, (size, sha256) in MODEL_ARTIFACT_DIGESTS.items():
        assert script_files[name]["size"] == size, name
        assert script_files[name]["sha256"] == sha256, name


def test_prepare_script_rerank_constants_match_runtime_identity() -> None:
    from citemind_inference.rerank_identity import (
        RERANK_MANIFEST_NAME,
        RERANK_MODEL_NAME,
        RERANK_MODEL_REVISION,
    )

    assert script_assignment("RERANK_MODEL") == RERANK_MODEL_NAME
    assert script_assignment("RERANK_REVISION") == RERANK_MODEL_REVISION
    assert script_assignment("RERANK_MANIFEST") == RERANK_MANIFEST_NAME
    # 构建期脚本不得写死未经下载核验的 reranker 摘要；摘要只由清单在构建期生成。
    assert "RERANK_EXPECTED_FILES" not in PREPARE_MODEL_SCRIPT.read_text(encoding="utf-8")
