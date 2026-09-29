"""测试用确定性 Embedder。

不加载任何权重，但遵守与真实实现相同的协议：确定性向量、真实计数语义、以及用于回归的
故障注入（损坏输出、阻塞闸门、并发计数）。
"""

import threading
import zlib
from collections.abc import Sequence
from typing import Any

from pydantic import SecretStr

from citemind_inference.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MAX_TOKENS,
    FROZEN_EMBEDDING_REVISION,
    FROZEN_RERANK_REVISION,
    RERANK_MAX_TOKENS,
    Settings,
)

TEST_TOKEN = "test-inference-token"
# 模拟 [CLS] 与 [SEP] 两个特殊 token。
SPECIAL_TOKEN_COUNT = 2

# 输出故障注入模式，用于验证输出契约校验确实生效。
CORRUPTIONS = ("nan", "inf", "negative_inf", "zeros", "short", "count", "scaled")


def build_settings(**overrides: Any) -> Settings:
    """构造测试 Settings，忽略环境变量与 .env 文件。"""

    values: dict[str, Any] = {
        "environment": "test",
        "inference_token": SecretStr(TEST_TOKEN),
    }
    values.update(overrides)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        **values,
    )


class StubEmbedder:
    """结果确定、可预测、可注入故障的 Embedder。

    token 计数固定为“字符数 + 2”（模拟两个特殊 token）；向量把 1.0 放在由文本 CRC32
    决定的位置，因此相同文本得到相同向量，不同文本几乎不会碰撞，可用于核对顺序。

    ``count_gate``/``embed_gate`` 用来把 CPU 阶段阻塞在确定的位置；``max_in_flight``
    记录 tokenize 与 encode 的真实并发峰值，用于验证准入上限。
    """

    def __init__(
        self,
        *,
        dimension: int = EMBEDDING_DIMENSION,
        model_revision: str = FROZEN_EMBEDDING_REVISION,
        max_tokens: int = EMBEDDING_MAX_TOKENS,
        token_overhead: int = SPECIAL_TOKEN_COUNT,
        count_gate: threading.Event | None = None,
        embed_gate: threading.Event | None = None,
        corrupt: str | None = None,
    ) -> None:
        if corrupt is not None and corrupt not in CORRUPTIONS:
            raise ValueError(f"未知的故障注入模式：{corrupt}")
        self._dimension = dimension
        self._model_revision = model_revision
        self._max_tokens = max_tokens
        self._token_overhead = token_overhead
        self._count_gate = count_gate
        self._embed_gate = embed_gate
        self._corrupt = corrupt
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.token_count_calls: list[list[str]] = []
        self.embed_calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_revision(self) -> str:
        return self._model_revision

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def vector_for(self, text: str) -> list[float]:
        """与 :meth:`embed` 完全一致的确定性向量，供测试推算期望值。"""

        vector = [0.0] * self._dimension
        vector[zlib.crc32(text.encode("utf-8")) % self._dimension] = 1.0
        return vector

    def _enter(self) -> None:
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)

    def _exit(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def token_counts(self, texts: Sequence[str]) -> list[int]:
        self.token_count_calls.append(list(texts))
        self._enter()
        try:
            if self._count_gate is not None:
                # 由测试显式放行，用来把 tokenize 阶段钉在并发许可内。
                self._count_gate.wait(timeout=10)
            return [len(text) + self._token_overhead for text in texts]
        finally:
            self._exit()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        self._enter()
        try:
            if self._embed_gate is not None:
                self._embed_gate.wait(timeout=10)
            return self._corrupt_vectors([self.vector_for(text) for text in texts])
        finally:
            self._exit()

    def _corrupt_vectors(self, vectors: list[list[float]]) -> list[list[float]]:
        match self._corrupt:
            case None:
                return vectors
            case "nan":
                vectors[0][0] = float("nan")
                return vectors
            case "inf":
                vectors[0][0] = float("inf")
                return vectors
            case "negative_inf":
                vectors[0][0] = float("-inf")
                return vectors
            case "zeros":
                return [[0.0] * self._dimension for _ in vectors]
            case "short":
                return [vector[:-1] for vector in vectors]
            case "count":
                return vectors[:-1]
            case "scaled":
                return [[value * 2.0 for value in vector] for vector in vectors]
        raise ValueError(f"未知的故障注入模式：{self._corrupt}")


RERANK_CORRUPTIONS = ("nan", "inf", "count")


class StubReranker:
    """确定性 reranker：不加载权重，按文本长度或显式分数返回，可注入故障。"""

    def __init__(
        self,
        *,
        model_revision: str = FROZEN_RERANK_REVISION,
        max_tokens: int = RERANK_MAX_TOKENS,
        scores: Sequence[float] | None = None,
        corrupt: str | None = None,
    ) -> None:
        if corrupt is not None and corrupt not in RERANK_CORRUPTIONS:
            raise ValueError(f"未知的故障注入模式：{corrupt}")
        self._model_revision = model_revision
        self._max_tokens = max_tokens
        self._scores = list(scores) if scores is not None else None
        self._corrupt = corrupt
        self.calls: list[tuple[str, list[str]]] = []

    @property
    def model_revision(self) -> str:
        return self._model_revision

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        scores = (
            list(self._scores)
            if self._scores is not None
            else [float(len(text)) for text in texts]
        )
        match self._corrupt:
            case None:
                return scores
            case "nan":
                scores[0] = float("nan")
                return scores
            case "inf":
                scores[0] = float("inf")
                return scores
            case "count":
                return scores[:-1]
        raise ValueError(f"未知的故障注入模式：{self._corrupt}")
