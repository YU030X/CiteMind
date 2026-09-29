"""基于 transformers + PyTorch 的本地 bge-reranker-base 实现。

按冻结契约实现：``XLMRobertaForSequenceClassification``，query 与候选组成 pair 输入，
``max_length=512`` 截断，取 ``logits.squeeze(-1)`` 作为原始分数（越高越相关），不做归一化、
不排序（排序由 API 侧完成）。模型只从本地目录加载（``local_files_only=True``），运行期绝不
联网下载；``HF_HUB_OFFLINE``/``TRANSFORMERS_OFFLINE`` 由镜像环境固定。
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import torch
from transformers import AutoConfig, AutoModelForSequenceClassification, AutoTokenizer

from citemind_inference.reranker import RerankModelError

EXPECTED_ARCHITECTURE = "XLMRobertaForSequenceClassification"


class TransformersReranker:
    """加载本地 reranker 权重并输出 pair 原始分数。"""

    def __init__(
        self,
        *,
        model_path: Path,
        model_name: str,
        model_revision: str,
        max_tokens: int,
        torch_threads: int,
    ) -> None:
        self._model_name = model_name
        self._model_revision = model_revision
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

        architectures = getattr(config, "architectures", None)
        if (
            isinstance(architectures, list)
            and architectures
            and EXPECTED_ARCHITECTURE not in architectures
        ):
            raise RerankModelError(
                f"{model_name}@{model_revision} 的 architectures={architectures!r}，"
                f"与冻结契约 {EXPECTED_ARCHITECTURE} 不一致"
            )

        model = AutoModelForSequenceClassification.from_pretrained(
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
    def model_revision(self) -> str:
        return self._model_revision

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        encoded: Any = self._tokenizer(
            [query] * len(texts),
            list(texts),
            add_special_tokens=True,
            truncation=True,
            max_length=self._max_tokens,
            padding=True,
            return_tensors="pt",
        )
        with torch.inference_mode():
            logits = self._model(**encoded).logits
        # 二分类头输出 (N, 1)；squeeze 后得到每条候选一个原始分数。
        scores = logits.squeeze(-1).float().cpu().tolist()
        return cast(list[float], scores)


__all__ = ["TransformersReranker"]
