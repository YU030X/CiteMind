"""reranker 的离线产物身份校验。

与 embedding 的 ``model_identity.py`` 刻意分开：``BAAI/bge-reranker-base`` 的六个产物在
本仓库内**没有**预先下载核验过的 SHA-256，因此这里不写死任何未经核对的摘要（不伪造身份）。
可信边界改为「构建期生成、运行期核对的产物清单」：

* 构建期 ``scripts/prepare_model.py --model rerank`` 从固定 HF revision 下载产物，逐个与
  Hub 报告的 ``size``、``blobId``（非 LFS）或 ``lfs.sha256``（LFS）交叉核验后，把实际字节
  大小与 SHA-256 写进模型目录父目录的 ``rerank-model-manifest.json``；
* 运行期只读该清单：要求声明的模型与 revision 等于冻结值，要求目录内文件集合与清单逐一
  对应，且每个文件的大小与 SHA-256 与清单一致。

这能拒绝「文件名对但字节被替换」「清单与目录不一致」「revision 漂移」；它**不能**独立证明
清单里的摘要一定来自官方 Hub——那需要联网。首次真实构建时须人工确认清单来源，并把结论记录
到 Agent Note。只依赖标准库，不导入 torch / transformers。
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from citemind_inference.config import FROZEN_RERANK_MODEL, FROZEN_RERANK_REVISION

RERANK_MODEL_NAME = FROZEN_RERANK_MODEL
RERANK_MODEL_REVISION = FROZEN_RERANK_REVISION

# 与 embedding 清单同名会冲突（同一父目录），因此 reranker 使用独立文件名。
RERANK_MANIFEST_NAME = "rerank-model-manifest.json"

_SHA256_HEX_LENGTH = 64


class RerankIdentityError(RuntimeError):
    """reranker 产物清单、文件集合或字节与冻结身份不符；调用方应转为启动失败。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(model_dir: Path) -> Path:
    """reranker 产物清单与模型目录同级：在模型目录的父目录下。"""

    return model_dir.parent / RERANK_MANIFEST_NAME


def _read_manifest(model_dir: Path) -> dict[str, Any]:
    path = manifest_path(model_dir)
    if not path.is_file():
        raise RerankIdentityError(
            f"缺少 reranker 产物清单：{path}；必须先以构建期脚本生成并核验"
        )
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RerankIdentityError(f"无法解析 reranker 产物清单：{path}") from error
    if not isinstance(parsed, dict):
        raise RerankIdentityError(f"reranker 产物清单不是 JSON 对象：{path}")
    return parsed


def _verify_manifest_identity(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if data.get("model") != RERANK_MODEL_NAME or data.get("revision") != RERANK_MODEL_REVISION:
        raise RerankIdentityError(
            f"reranker 产物清单声明的身份不是 {RERANK_MODEL_NAME}@{RERANK_MODEL_REVISION}"
        )
    records = data.get("files")
    if not isinstance(records, dict) or not records:
        raise RerankIdentityError("reranker 产物清单缺少非空 files")
    resolved: dict[str, dict[str, Any]] = {}
    for name, record in records.items():
        if not isinstance(name, str) or not name or not isinstance(record, dict):
            raise RerankIdentityError("reranker 产物清单条目不合法")
        size = record.get("size")
        sha256 = record.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RerankIdentityError(f"reranker 产物清单缺少合法 size：{name}")
        if (
            not isinstance(sha256, str)
            or len(sha256) != _SHA256_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RerankIdentityError(f"reranker 产物清单缺少合法 SHA-256：{name}")
        resolved[name] = {"size": size, "sha256": sha256}
    return resolved


def _verify_directory_files(model_dir: Path, records: dict[str, dict[str, Any]]) -> None:
    actual = {entry.name for entry in model_dir.iterdir() if entry.is_file()}
    expected = set(records)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        raise RerankIdentityError(
            f"reranker 模型目录 {model_dir} 的文件集合不符；"
            f"缺失 {missing or '无'}，额外 {extra or '无'}"
        )
    for name, record in records.items():
        path = model_dir / name
        if path.stat().st_size != record["size"]:
            raise RerankIdentityError(f"reranker 模型文件大小不符：{name}")
        if sha256_file(path) != record["sha256"]:
            raise RerankIdentityError(f"reranker 模型文件 SHA-256 不符：{name}")


def verify_rerank_artifacts(model_dir: Path) -> None:
    """离线核验 reranker 模型目录：清单身份、文件集合与逐文件摘要都必须一致。"""

    if not model_dir.is_dir():
        raise RerankIdentityError(f"reranker 模型目录不存在：{model_dir}")
    data = _read_manifest(model_dir)
    records = _verify_manifest_identity(data)
    _verify_directory_files(model_dir, records)


__all__ = [
    "RERANK_MANIFEST_NAME",
    "RERANK_MODEL_NAME",
    "RERANK_MODEL_REVISION",
    "RerankIdentityError",
    "manifest_path",
    "sha256_file",
    "verify_rerank_artifacts",
]
