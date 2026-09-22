"""Embedder 抽象与本地模型加载入口。

真实编码器实现在 :mod:`citemind_inference.transformers_embedder`，那里才会导入 torch 与
transformers。本模块只依赖协议与配置，因此单元测试注入 stub 时不会加载任何权重。
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from citemind_inference.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MAX_TOKENS,
    FROZEN_EMBEDDING_MODEL,
    Settings,
)
from citemind_inference.model_identity import (
    ModelIdentityError,
    verify_model_artifacts,
)

# transformers 从本地目录加载时必须能看到的文件；缺一个都说明权重不完整。
REQUIRED_MODEL_FILES = ("config.json", "tokenizer_config.json", "vocab.txt")
MODEL_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


class EmbeddingModelError(RuntimeError):
    """本地模型缺失或不满足冻结编码契约；由 lifespan 转成启动失败。"""


class Embedder(Protocol):
    """可注入的编码器接口。

    ``max_tokens`` 是模型自身的硬上限；调用方还应叠加配置上限一起判断。
    """

    @property
    def dimension(self) -> int: ...

    @property
    def model_revision(self) -> str: ...

    @property
    def max_tokens(self) -> int: ...

    def token_counts(self, texts: Sequence[str]) -> list[int]:
        """返回每条文本的真实 token 数，包含特殊 token，且不做截断。"""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序返回 L2 归一化向量。"""
        ...


EmbedderFactory = Callable[[Settings], Embedder]


def inspect_model_directory(settings: Settings) -> Path:
    """在导入 torch 之前先做纯文件系统与字节校验，失败信息更直接。

    除路径存在性外，还离线核对固定 revision 的六个产物：文件集合、大小与 SHA-256，
    并与模型目录父目录的 ``model-manifest.json`` 交叉验证。覆盖路径同样走这套可信字节
    校验，因此换目录不会绕过身份约束。任何偏差都抛出 :class:`EmbeddingModelError`。
    """

    path = settings.embedding_model_path
    if not path.is_dir():
        raise EmbeddingModelError(
            f"本地 embedding 模型目录不存在：{path}；本服务不从网络下载模型"
        )
    missing = [name for name in REQUIRED_MODEL_FILES if not (path / name).is_file()]
    if missing:
        raise EmbeddingModelError(f"模型目录 {path} 缺少必需文件：{', '.join(missing)}")
    if not any((path / name).is_file() for name in MODEL_WEIGHT_FILES):
        raise EmbeddingModelError(
            f"模型目录 {path} 缺少权重文件（{' 或 '.join(MODEL_WEIGHT_FILES)}）"
        )
    try:
        verify_model_artifacts(path)
    except ModelIdentityError as error:
        raise EmbeddingModelError(str(error)) from error
    return path


def load_embedder(settings: Settings) -> Embedder:
    """加载冻结 revision 的本地模型；任何缺失或校验不符都抛出，使启动失败。"""

    path = inspect_model_directory(settings)
    # 重量级依赖只在真正加载模型时导入；单测注入 stub 不会触发 torch。
    from citemind_inference.transformers_embedder import TransformersEmbedder

    return TransformersEmbedder(
        model_path=path,
        model_name=FROZEN_EMBEDDING_MODEL,
        model_revision=settings.embedding_model_revision,
        dimension=EMBEDDING_DIMENSION,
        max_tokens=EMBEDDING_MAX_TOKENS,
        torch_threads=settings.embedding_torch_threads,
    )
