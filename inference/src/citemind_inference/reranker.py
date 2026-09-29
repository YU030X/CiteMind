"""Reranker 抽象与本地模型加载入口。

真实 reranker 实现在 :mod:`citemind_inference.transformers_reranker`，那里才会导入 torch 与
transformers。本模块只依赖协议与配置，因此单元测试注入 stub 时不会加载任何权重。

与 embedding 不同，reranker 默认关闭：``RERANK_ENABLED=0`` 时既不校验模型目录也不加载权重，
``/capabilities`` 如实报 ``rerank.ready=false``，``POST /internal/rerank`` 静态 503。显式开启
时启动阶段要求模型目录与产物清单完整，否则 fail fast。
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from citemind_inference.config import FROZEN_RERANK_MODEL, RERANK_MAX_TOKENS, Settings
from citemind_inference.rerank_identity import (
    RerankIdentityError,
    verify_rerank_artifacts,
)

# transformers 从本地目录加载时必须能看到的文件；缺一个都说明权重不完整。
REQUIRED_RERANK_FILES = (
    "config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
)
RERANK_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


class RerankModelError(RuntimeError):
    """本地 reranker 缺失或不满足冻结契约；由 lifespan 转成启动失败。"""


class Reranker(Protocol):
    """可注入的 reranker 接口。"""

    @property
    def model_revision(self) -> str: ...

    @property
    def max_tokens(self) -> int: ...

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """按输入顺序返回 query 与每条 text 的原始相关性分数（不排序）。"""
        ...


RerankerFactory = Callable[[Settings], Reranker]


def inspect_rerank_model_directory(settings: Settings) -> Path:
    """在导入 torch 之前先做纯文件系统与字节校验，失败信息更直接。"""

    path = settings.rerank_model_path
    if not path.is_dir():
        raise RerankModelError(
            f"本地 reranker 模型目录不存在：{path}；本服务不从网络下载模型"
        )
    missing = [name for name in REQUIRED_RERANK_FILES if not (path / name).is_file()]
    if missing:
        raise RerankModelError(f"reranker 模型目录 {path} 缺少必需文件：{', '.join(missing)}")
    if not any((path / name).is_file() for name in RERANK_WEIGHT_FILES):
        raise RerankModelError(
            f"reranker 模型目录 {path} 缺少权重文件（{' 或 '.join(RERANK_WEIGHT_FILES)}）"
        )
    try:
        verify_rerank_artifacts(path)
    except RerankIdentityError as error:
        raise RerankModelError(str(error)) from error
    return path


def load_reranker(settings: Settings) -> Reranker:
    """加载冻结 revision 的 reranker；任何缺失或校验不符都抛出，使启动失败。"""

    path = inspect_rerank_model_directory(settings)
    # 重量级依赖只在真正加载模型时导入；单测注入 stub 不会触发 torch。
    from citemind_inference.transformers_reranker import TransformersReranker

    return TransformersReranker(
        model_path=path,
        model_name=FROZEN_RERANK_MODEL,
        model_revision=settings.rerank_model_revision,
        max_tokens=RERANK_MAX_TOKENS,
        torch_threads=settings.embedding_torch_threads,
    )


__all__ = [
    "REQUIRED_RERANK_FILES",
    "RERANK_WEIGHT_FILES",
    "RerankModelError",
    "Reranker",
    "RerankerFactory",
    "inspect_rerank_model_directory",
    "load_reranker",
]
