"""冻结的模型身份与可信字节校验。

模型名与 revision 复用 :mod:`citemind_inference.config` 的冻结契约，本模块只补齐六个可信
产物的字节大小与 SHA-256，并提供离线校验入口。只依赖标准库，不导入 torch / transformers，
因此加载端可以在导入重量级依赖之前完成校验。

可信身份以本文件钉死的摘要为准；模型目录父目录的 ``model-manifest.json`` 只是待核对产物，
不能自报身份。构建期脚本 ``scripts/prepare_model.py`` 使用同一组常量，二者由
``tests/test_model_identity.py`` 的防漂移测试约束一致。
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from citemind_inference.config import FROZEN_EMBEDDING_MODEL, FROZEN_EMBEDDING_REVISION

MODEL_NAME = FROZEN_EMBEDDING_MODEL
MODEL_REVISION = FROZEN_EMBEDDING_REVISION

# 产物清单放在模型目录之外：它只描述产物身份，不参与模型目录自身的文件集合校验。
MANIFEST_NAME = "model-manifest.json"

# 六个可信产物在固定 revision 下的实际字节数与 SHA-256；任何偏差都必须使加载失败。
MODEL_ARTIFACT_DIGESTS: dict[str, tuple[int, str]] = {
    "config.json": (
        776,
        "3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a",
    ),
    "tokenizer_config.json": (
        367,
        "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    ),
    "tokenizer.json": (
        439125,
        "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    ),
    "special_tokens_map.json": (
        125,
        "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    ),
    "vocab.txt": (
        109540,
        "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
    ),
    "model.safetensors": (
        95827648,
        "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026",
    ),
}

# 固定顺序的产物名集合；目录内多一个或少一个都视为不可信。
MODEL_ARTIFACT_FILES: tuple[str, ...] = tuple(MODEL_ARTIFACT_DIGESTS)


class ModelIdentityError(RuntimeError):
    """模型产物身份、集合或字节与冻结契约不符；调用方应转为启动失败。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(model_dir: Path) -> Path:
    """产物清单与模型目录同级：在模型目录的父目录下。"""

    return model_dir.parent / MANIFEST_NAME


def _read_manifest(model_dir: Path) -> dict[str, Any]:
    path = manifest_path(model_dir)
    if not path.is_file():
        raise ModelIdentityError(f"缺少模型产物清单：{path}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelIdentityError(f"无法解析模型产物清单：{path}") from error
    if not isinstance(parsed, dict):
        raise ModelIdentityError(f"模型产物清单不是 JSON 对象：{path}")
    return parsed


def _verify_manifest(model_dir: Path) -> None:
    """把清单当作待核对对象：清单里的一切都必须与本文件钉死的值一致。"""

    data = _read_manifest(model_dir)
    if data.get("model") != MODEL_NAME or data.get("revision") != MODEL_REVISION:
        raise ModelIdentityError(
            f"模型产物清单声明的身份不是 {MODEL_NAME}@{MODEL_REVISION}"
        )
    records = data.get("files")
    if not isinstance(records, dict) or set(records) != set(MODEL_ARTIFACT_DIGESTS):
        raise ModelIdentityError("模型产物清单的文件集合不符")
    for name, (size, sha256) in MODEL_ARTIFACT_DIGESTS.items():
        record = records[name]
        if not isinstance(record, dict):
            raise ModelIdentityError(f"模型产物清单条目不是对象：{name}")
        if record.get("size") != size or record.get("sha256") != sha256:
            raise ModelIdentityError(f"产物清单的摘要与钉死值不符：{name}")


def _verify_directory_files(model_dir: Path) -> None:
    actual = {entry.name for entry in model_dir.iterdir()}
    expected = set(MODEL_ARTIFACT_DIGESTS)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        raise ModelIdentityError(
            f"模型目录 {model_dir} 的文件集合不符；缺失 {missing or '无'}，额外 {extra or '无'}"
        )
    for name, (size, sha256) in MODEL_ARTIFACT_DIGESTS.items():
        path = model_dir / name
        if path.stat().st_size != size:
            raise ModelIdentityError(f"模型文件大小不符：{name}")
        if sha256_file(path) != sha256:
            raise ModelIdentityError(f"模型文件 SHA-256 不符：{name}")


def verify_model_artifacts(model_dir: Path) -> None:
    """离线核验模型目录：清单与钉死摘要一致，且目录内恰为这六个可信文件。"""

    if not model_dir.is_dir():
        raise ModelIdentityError(f"模型目录不存在：{model_dir}")
    _verify_manifest(model_dir)
    _verify_directory_files(model_dir)
