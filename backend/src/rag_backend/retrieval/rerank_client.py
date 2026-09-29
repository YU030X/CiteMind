"""API 侧受限内部重排客户端（可降级）。

只把「已授权的 RRF top-10 候选」交给内部 inference 的 ``POST /internal/rerank``。与查询编码
客户端复用同一安全边界：同步 ``httpx.Client``、``trust_env=False``、``retries=0``、显式不跟随
重定向、显式 ``Accept-Encoding: identity``、Bearer token 只发往内部 inference 或测试回环
地址、成功响应有界流式读取（上限 1 MiB）。客户端自身零自动重试。

与查询编码不同，重排是可选增强：**任何**失败（超时、连接、非 2xx、响应超限/非法 JSON、
集合不完全、重复 id、非 finite 分数、revision 不匹配、本地长度/字节超限）都收敛为同一种
可降级异常 :class:`RerankUnavailableError`。调用方据此保持原 RRF 顺序并标记
``rerank_unavailable``，绝不伪造重排分数。错误消息静态，不回显查询文本、候选正文、token 或
内部 URL。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import TracebackType

import httpx
from pydantic import SecretStr

from rag_backend.config import (
    DEFAULT_INFERENCE_BASE_URL,
    DEFAULT_RERANK_TIMEOUT_SECONDS,
    Settings,
)
from rag_backend.retrieval.query_embedding_client import (
    QueryEmbeddingResponseError,
    _declared_length_exceeds,
    _has_foreign_content_encoding,
    _read_capped,
    _resolve_token,
    validate_inference_base_url,
)

# 冻结的 reranker 契约；与 inference 的 FROZEN_RERANK_REVISION 一致。
EXPECTED_RERANK_MODEL_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"

RERANK_ENDPOINT = "/internal/rerank"
REQUEST_QUERY_KEY = "query"
REQUEST_CANDIDATES_KEY = "candidates"
REQUEST_CANDIDATE_ID_KEY = "candidateId"
REQUEST_TEXT_KEY = "text"
RESPONSE_SCORES_KEY = "scores"
RESPONSE_CANDIDATE_ID_KEY = "candidateId"
RESPONSE_SCORE_KEY = "score"
RESPONSE_MODEL_REVISION_KEY = "modelRevision"

# 与 inference 语义预算一致的本地上限：最多 10 条候选，query/单条文本各自有字符上限，
# 合计有字节上限。超限不截断，直接降级（不发请求）。
MAX_CANDIDATES = 10
MAX_QUERY_CHARS = 4000
MAX_TEXT_CHARS = 8000
MAX_TOTAL_BYTES = 262144
# 成功响应体上限；Content-Length 只用于早拒，真实字节仍按流累计。
MAX_RESPONSE_BODY_BYTES = 1024 * 1024

HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class RerankInput:
    """一条待重排候选：``candidate_id`` 由调用方提供，服务端只回传对应分数。"""

    candidate_id: str
    text: str


@dataclass(frozen=True, slots=True)
class RerankScore:
    """一条候选的原始相关性分数；不排序、不归一化。"""

    candidate_id: str
    score: float


class RerankUnavailableError(Exception):
    """统一的、可降级的重排失败；调用方必须回退原 RRF 顺序并标记降级。"""


def _validate_rerank_input(query: str, candidates: list[RerankInput]) -> None:
    """本地 failfast：任何不合上限的输入都在发请求前转为可降级异常。"""

    if not isinstance(query, str) or not query.strip():
        raise RerankUnavailableError("重排查询为空")
    if len(query) > MAX_QUERY_CHARS:
        raise RerankUnavailableError("重排查询超过本地长度上限")
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise RerankUnavailableError("重排候选条数不在允许范围")
    ids = [candidate.candidate_id for candidate in candidates]
    if len(set(ids)) != len(ids) or any(not candidate_id for candidate_id in ids):
        raise RerankUnavailableError("重排候选 id 非法或重复")
    try:
        total_bytes = len(query.encode("utf-8"))
    except UnicodeEncodeError:
        raise RerankUnavailableError("重排查询含非法 Unicode") from None
    for candidate in candidates:
        if not candidate.text.strip():
            raise RerankUnavailableError("重排候选正文为空")
        if len(candidate.text) > MAX_TEXT_CHARS:
            raise RerankUnavailableError("重排候选正文超过本地长度上限")
        try:
            total_bytes += len(candidate.text.encode("utf-8"))
        except UnicodeEncodeError:
            raise RerankUnavailableError("重排候选含非法 Unicode") from None
    if total_bytes > MAX_TOTAL_BYTES:
        raise RerankUnavailableError("重排文本合计超过本地字节上限")


def _request_body(query: str, candidates: list[RerankInput]) -> bytes:
    return json.dumps(
        {
            REQUEST_QUERY_KEY: query,
            REQUEST_CANDIDATES_KEY: [
                {
                    REQUEST_CANDIDATE_ID_KEY: candidate.candidate_id,
                    REQUEST_TEXT_KEY: candidate.text,
                }
                for candidate in candidates
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_rerank_response(
    payload: object, candidates: list[RerankInput]
) -> list[RerankScore]:
    """严格核对响应契约：revision、条数、集合完全、id 不重复、分数有限。"""

    if not isinstance(payload, dict):
        raise RerankUnavailableError("内部重排响应结构不合法")
    if payload.get(RESPONSE_MODEL_REVISION_KEY) != EXPECTED_RERANK_MODEL_REVISION:
        raise RerankUnavailableError("内部重排响应模型 revision 不匹配")
    raw_scores = payload.get(RESPONSE_SCORES_KEY)
    expected_ids = {candidate.candidate_id for candidate in candidates}
    if not isinstance(raw_scores, list) or len(raw_scores) != len(expected_ids):
        raise RerankUnavailableError("内部重排响应分数条数不匹配")

    resolved: list[RerankScore] = []
    seen: set[str] = set()
    for item in raw_scores:
        if not isinstance(item, dict):
            raise RerankUnavailableError("内部重排响应分数结构不合法")
        candidate_id = item.get(RESPONSE_CANDIDATE_ID_KEY)
        if not isinstance(candidate_id, str) or candidate_id not in expected_ids:
            raise RerankUnavailableError("内部重排响应含未知 candidateId")
        if candidate_id in seen:
            raise RerankUnavailableError("内部重排响应含重复 candidateId")
        seen.add(candidate_id)
        value = item.get(RESPONSE_SCORE_KEY)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RerankUnavailableError("内部重排响应分数非数值")
        score = float(value)
        if not math.isfinite(score):
            raise RerankUnavailableError("内部重排响应分数含 NaN 或 Inf")
        resolved.append(RerankScore(candidate_id=candidate_id, score=score))
    if seen != expected_ids:
        raise RerankUnavailableError("内部重排响应候选集合不完全")
    return resolved


class RerankClient:
    """受限内部重排客户端；同步、无自动重试、资源所有权显式。"""

    def __init__(
        self,
        *,
        token: SecretStr | str | None = None,
        base_url: str = DEFAULT_INFERENCE_BASE_URL,
        timeout_seconds: float = DEFAULT_RERANK_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("内部重排超时必须是有限正数")
        resolved_token = _resolve_token(token)
        self._base_url = validate_inference_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._owns_client = transport is None
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=(
                transport
                if transport is not None
                else httpx.HTTPTransport(retries=0, trust_env=False)
            ),
            trust_env=False,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {resolved_token}",
                # 只接受未压缩响应；与服务端默认无压缩行为一致，避免解压炸弹。
                "Accept-Encoding": "identity",
            },
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> RerankClient:
        """用进程配置构造客户端；未配置 ``INFERENCE_TOKEN`` 时构造即失败。"""

        return cls(
            token=settings.inference_token,
            base_url=settings.inference_base_url,
            timeout_seconds=settings.rerank_timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> RerankClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """仅在自建 transport 时关闭自有 Client；注入 transport 时本调用是 no-op。"""

        if self._owns_client:
            self._client.close()

    def rerank(self, query: str, candidates: list[RerankInput]) -> list[RerankScore]:
        """对候选打分；任何失败都抛 :class:`RerankUnavailableError`，不返回部分结果。"""

        _validate_rerank_input(query, candidates)
        body = _request_body(query, candidates)
        try:
            with self._client.stream(
                "POST",
                RERANK_ENDPOINT,
                content=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != HTTP_OK:
                    # 任何非 2xx（含 404/413/422/503）都统一降级，不解析错误正文。
                    raise RerankUnavailableError("内部重排服务返回非成功状态")
                if _has_foreign_content_encoding(response):
                    raise RerankUnavailableError("内部重排响应使用了不受支持的压缩编码")
                if _declared_length_exceeds(response, MAX_RESPONSE_BODY_BYTES):
                    raise RerankUnavailableError("内部重排响应超出上限")
                raw = _read_capped(response, MAX_RESPONSE_BODY_BYTES, overflow_is_error=True)
        except httpx.TimeoutException:
            raise RerankUnavailableError("内部重排请求超时") from None
        except httpx.RequestError:
            raise RerankUnavailableError("内部重排连接失败") from None
        except QueryEmbeddingResponseError:
            raise RerankUnavailableError("内部重排响应超出上限") from None

        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            raise RerankUnavailableError("内部重排响应不是合法 JSON") from None
        return _validate_rerank_response(payload, candidates)


__all__ = [
    "EXPECTED_RERANK_MODEL_REVISION",
    "MAX_CANDIDATES",
    "MAX_QUERY_CHARS",
    "MAX_RESPONSE_BODY_BYTES",
    "MAX_TEXT_CHARS",
    "MAX_TOTAL_BYTES",
    "RERANK_ENDPOINT",
    "RerankClient",
    "RerankInput",
    "RerankScore",
    "RerankUnavailableError",
]
