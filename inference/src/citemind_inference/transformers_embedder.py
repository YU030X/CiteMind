"""基于 transformers + PyTorch 的本地 BGE 编码器。

按冻结契约实现：CLS pooling + L2 normalize，512 维，CPU，直连 transformers，不使用
sentence-transformers。模型只从本地目录加载（``local_files_only=True``），运行期绝
不联网下载。
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import torch
from transformers import AutoConfig, AutoModel, AutoTokenizer

from citemind_inference.embeddings import EmbeddingModelError


class TransformersEmbedder:
    """加载本地 BGE 权重并做 CLS + L2 归一化编码。"""

    def __init__(
        self,
        *,
        model_path: Path,
        model_name: str,
        model_revision: str,
        dimension: int,
        max_tokens: int,
        torch_threads: int,
    ) -> None:
        self._model_name = model_name
        self._model_revision = model_revision
        self._dimension = dimension
        self._max_tokens = max_tokens

        # 进程只加载一份权重；线程数在启动时固定，避免每请求各自抢占 CPU。
        torch.set_num_threads(torch_threads)

        local_path = str(model_path)
        # 只允许本地文件，显式禁止 remote code，并固定 fast tokenizer 与 safetensors 权重。
        self._tokenizer = AutoTokenizer.from_pretrained(
            local_path,
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
        config = AutoConfig.from_pretrained(
            local_path,
            local_files_only=True,
            trust_remote_code=False,
        )

        hidden_size = getattr(config, "hidden_size", None)
        if hidden_size != dimension:
            raise EmbeddingModelError(
                f"{model_name}@{model_revision} 的 hidden_size={hidden_size!r}，"
                f"与冻结维度 {dimension} 不一致"
            )

        model_max_length = getattr(self._tokenizer, "model_max_length", None)
        if isinstance(model_max_length, int) and 0 < model_max_length < max_tokens:
            raise EmbeddingModelError(
                f"{model_name}@{model_revision} 的 tokenizer 最大长度 {model_max_length} "
                f"小于要求的 {max_tokens}"
            )

        model = AutoModel.from_pretrained(
            local_path,
            config=config,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.float32,
        )
        model.eval()
        self._model = model

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_revision(self) -> str:
        return self._model_revision

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def token_counts(self, texts: Sequence[str]) -> list[int]:
        """真实 token 数，含特殊 token；显式关闭截断和填充。"""

        encoded: Any = self._tokenizer(
            list(texts),
            add_special_tokens=True,
            truncation=False,
            padding=False,
        )
        return [len(input_ids) for input_ids in encoded["input_ids"]]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        encoded: Any = self._tokenizer(
            list(texts),
            add_special_tokens=True,
            truncation=False,
            padding=True,
            return_tensors="pt",
        )
        with torch.inference_mode():
            outputs = self._model(**encoded)
        # BGE v1.5 使用 CLS token 的最后一层隐状态作为句向量。
        cls_vectors = outputs.last_hidden_state[:, 0, :]
        normalized = torch.nn.functional.normalize(cls_vectors.float(), p=2, dim=1)
        return cast(list[list[float]], normalized.cpu().tolist())
