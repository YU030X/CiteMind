"""Worker 本地真实 token 计数：只从已烘入镜像的 BGE 模型目录加载并校验 tokenizer 产物。

本模块只做纯本地计算：不联网、不读环境密钥、不把输入文本写进日志、不接触数据库、Redis 或
Celery。它实现 :class:`rag_backend.ingestion.chunking.TokenCounter` 协议，供切分器测量完整
模型输入的真实 token 数。worker 镜像只从 inference 镜像构建产物复制四个 tokenizer 文件；
每个文件都按冻结 revision 的字节大小与 SHA-256 逐一校验，目录内多一个少一个都视为不可信，
失败时抛出 :class:`TokenCounterError`。构造成功即代表字节校验通过；实例在单个 worker 内
长期复用，绝不为每次调用重建 tokenizer。

身份不得自报：这里不读取 inference 的 ``model-manifest.json``，只信本文件钉死的摘要与目录
实际内容。摘要与 ``inference/src/citemind_inference/model_identity.py`` 的同名条目一致，
由 ``tests/unit/test_token_counting.py`` 的防漂移测试约束。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final

from tokenizers import Tokenizer

# worker 镜像从 inference 产物复制的模型目录；与 inference 冻结路径一致。
DEFAULT_MODEL_DIRECTORY: Final = Path("/models/bge-small-zh-v1.5")

# 固定 revision 下四个 tokenizer 产物的字节数与 SHA-256。这里只钉住计数所需文件，
# 不含 config.json 与 model.safetensors：worker 不做推理，也不需要 torch。
TOKENIZER_ARTIFACTS: Final[dict[str, tuple[int, str]]] = {
    "tokenizer.json": (
        439125,
        "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    ),
    "vocab.txt": (
        109540,
        "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
    ),
    "tokenizer_config.json": (
        367,
        "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    ),
    "special_tokens_map.json": (
        125,
        "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    ),
}


class TokenCounterError(RuntimeError):
    """tokenizer 产物缺失、文件集合不符或字节摘要不符；调用方应转为启动失败。"""


def _io_failure_reason(error: OSError) -> str:
    """把 OSError 类型映射为静态原因，绝不回显异常文本里的外部路径。"""

    if isinstance(error, FileNotFoundError):
        return "文件不存在"
    if isinstance(error, PermissionError):
        return "权限不足"
    if isinstance(error, IsADirectoryError):
        return "不是常规文件"
    return "IO 错误"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_tokenizer_directory(
    model_directory: Path,
    expected: Mapping[str, tuple[int, str]] | None = None,
) -> None:
    """核验目录内恰为 ``expected`` 声明的文件集合，且逐文件大小与 SHA-256 一致。

    ``expected`` 省略时使用本模块钉死的 :data:`TOKENIZER_ARTIFACTS`；显式传入只用于测试
    隔离，生产构造路径始终使用钉死值。所有 IO 失败都归一为静态 :class:`TokenCounterError`，
    消息只含钉死产物名与错误类别，不含外部路径或产物正文。
    """

    table = TOKENIZER_ARTIFACTS if expected is None else expected
    try:
        # is_dir() 自身就可能抛 PermissionError（父目录不可读等），必须与 iterdir 一起包住。
        if not model_directory.is_dir():
            raise TokenCounterError("tokenizer 模型目录不存在")
        actual = {entry.name for entry in model_directory.iterdir()}
    except TokenCounterError:
        raise
    except OSError as error:
        raise TokenCounterError(
            f"tokenizer 模型目录不可读（{_io_failure_reason(error)}）"
        ) from None
    expected_names = set(table)
    extra = sorted(actual - expected_names)
    missing = sorted(expected_names - actual)
    if extra or missing:
        # 只报数量：目录里的文件名可能来自外部，不回显到错误消息里。
        raise TokenCounterError(
            f"tokenizer 模型目录文件集合不符；缺失 {len(missing)} 个，额外 {len(extra)} 个"
        )
    for name, (size, sha256) in table.items():
        path = model_directory / name
        try:
            actual_size = path.stat().st_size
            actual_sha256 = sha256_file(path)
        except OSError as error:
            raise TokenCounterError(
                f"tokenizer 文件不可读：{name}（{_io_failure_reason(error)}）"
            ) from None
        if actual_size != size:
            raise TokenCounterError(f"tokenizer 文件大小不符：{name}")
        if actual_sha256 != sha256:
            raise TokenCounterError(f"tokenizer 文件 SHA-256 不符：{name}")


class LocalTokenizerCounter:
    """单个 worker 进程内复用的真实 token 计数器；构造时完成字节校验。

    构造顺序固定为先校验、后加载：任何损坏产物都必须在校验阶段以
    :class:`TokenCounterError` 失败，而不是等到 ``tokenizers`` 解析时才报底层错误。
    加载后显式 ``no_truncation()``，即使 ``tokenizer.json`` 自带 truncation 配置也按
    完整文本计数；底层 IO/解析异常统一归一为静态 :class:`TokenCounterError`，不保留
    含路径的异常链。
    """

    def __init__(self, model_directory: Path = DEFAULT_MODEL_DIRECTORY) -> None:
        verify_tokenizer_directory(model_directory)
        try:
            tokenizer = Tokenizer.from_file(str(model_directory / "tokenizer.json"))
            tokenizer.no_truncation()
        except OSError as error:
            raise TokenCounterError(
                f"tokenizer 产物加载失败（{_io_failure_reason(error)}）"
            ) from None
        except Exception:
            raise TokenCounterError("tokenizer 产物解析失败") from None
        self._tokenizer = tokenizer

    def count_tokens(self, text: str) -> int:
        """返回含特殊 token 的完整 token 数，完全不截断。"""

        return len(self._tokenizer.encode(text, add_special_tokens=True).ids)


@lru_cache(maxsize=1)
def get_worker_token_counter() -> LocalTokenizerCounter:
    """返回 worker 进程内唯一的计数器；首次调用即完成校验，之后复用同一实例。

    与 :func:`rag_backend.config.get_settings` 一致，用进程级缓存把实例生命周期钉在单个
    worker 上；当前入口只用于本地真实计数，尚未接入任何 Celery 任务。
    """

    return LocalTokenizerCounter(DEFAULT_MODEL_DIRECTORY)
