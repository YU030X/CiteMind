"""业务问答的受限 DeepSeek 客户端：固定端点、非流式、thinking 关闭、零自动重试。

出站约束与一次性探针一致并更严格：

- 只连固定 ``https://api.deepseek.com`` 的 ``/chat/completions``，不接受任意 base_url；
- 显式 ``thinking: {"type": "disabled"}``、``stream: false`` 与 ``max_tokens``，不依赖供应商默认；
- ``trust_env=False``，不继承宿主 ``HTTP(S)_PROXY``/自定义 CA；``retries=0``，客户端零自动重试；
- 成功体流式读取且有界（超过上限判 ``INVALID_RESPONSE``），非 2xx 不读正文；
- 成功体非 UTF-8、或被 ``httpx`` 判定为解压/内容解码失败时也判 ``INVALID_RESPONSE`` 失败事实，
  绝不让底层解码异常冒泡中断 ``llm_usage`` 记账；
- provider 未报告 usage 或 usage 非法时不报成功，token 一律留空；
- ``finish_reason == "length"`` 明确判为 ``TRUNCATED`` 失败，绝不把截断内容当完整回答。

本模块不做任何权限判断、不读写数据库、不记录提示正文或密钥。真实用量必须由调用方按本模块
返回的事实追加到 ``llm_usage``；本地 token 估算永远不能写成 provider 事实。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from rag_backend.config import Settings
from rag_backend.generation.deepseek_prompt import ChatMessage

PROVIDER = "deepseek"
# 供应商权威文档指定的生产 endpoint；不暴露为配置项，避免以任意 base_url 伪装真实验收。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CHAT_COMPLETIONS_PATH = "/chat/completions"
ANSWER_STAGE = "qa_answer"
NETWORK_RETRIES = 0

STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_TIMEOUT = "TIMEOUT"
USAGE_PROVIDER_REPORTED = "PROVIDER_REPORTED"
USAGE_UNKNOWN = "UNKNOWN"


class GenerationConfigError(RuntimeError):
    """未开启业务生成或缺少可用密钥；调用方应映射为静态 503，且不联网。"""


class _ProviderHTTPError(Exception):
    """非 2xx 响应；只保留状态码，不读正文、不回显供应商消息。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(str(status_code))
        self.status_code = status_code


@dataclass(frozen=True)
class GenerationOutcome:
    """一次 provider attempt 的最终事实；``content`` 仅在成功时有值。"""

    status: str
    error_code: str | None
    usage_source: str
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    latency_ms: int
    content: str | None


class AnswerGenerator(Protocol):
    """同步生成接口；``DeepSeekAnswerGenerator`` 在结构上满足它，测试可注入假实现。"""

    def generate(
        self, messages: Sequence[ChatMessage], *, max_output_tokens: int
    ) -> GenerationOutcome: ...

    def close(self) -> None: ...


def build_chat_payload(
    model: str, messages: Sequence[ChatMessage], *, max_output_tokens: int
) -> dict[str, Any]:
    """固定业务请求体：非流式、显式关闭 thinking，并限制输出长度。"""

    return {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content} for message in messages
        ],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": max_output_tokens,
    }


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


# provider usage 数值分类：ok 可用于落账，absent 可写 NULL，invalid 必须整行判失败。
_TOKEN_OK = "ok"
_TOKEN_ABSENT = "absent"
_TOKEN_INVALID = "invalid"


def _classify_usage_token(value: Any) -> tuple[str, int | None]:
    """分类 provider usage 数值；bool、负数与非整数均视为无效，绝不落库。"""

    if value is None:
        return _TOKEN_ABSENT, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return _TOKEN_INVALID, None
    return _TOKEN_OK, int(value)


@dataclass(frozen=True)
class _Usage:
    source: str
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    error_code: str | None


def _interpret_usage(usage: Any) -> _Usage:
    """从响应 ``usage`` 提取 provider 事实；缺失或非法都返回 UNKNOWN 与错误码。"""

    if not isinstance(usage, dict):
        return _Usage(USAGE_UNKNOWN, None, None, None, None, "MISSING_USAGE")
    prompt_state, prompt_tokens = _classify_usage_token(usage.get("prompt_tokens"))
    completion_state, completion_tokens = _classify_usage_token(usage.get("completion_tokens"))
    hit_state, hit_tokens = _classify_usage_token(usage.get("prompt_cache_hit_tokens"))
    miss_state, miss_tokens = _classify_usage_token(usage.get("prompt_cache_miss_tokens"))
    if _TOKEN_INVALID in (prompt_state, completion_state, hit_state, miss_state):
        return _Usage(USAGE_UNKNOWN, None, None, None, None, "INVALID_USAGE")
    if prompt_state == _TOKEN_ABSENT or completion_state == _TOKEN_ABSENT:
        return _Usage(USAGE_UNKNOWN, None, None, None, None, "MISSING_USAGE")
    return _Usage(
        USAGE_PROVIDER_REPORTED,
        prompt_tokens,
        completion_tokens,
        hit_tokens,
        miss_tokens,
        None,
    )


def _failure(error_code: str, latency_ms: int) -> GenerationOutcome:
    return GenerationOutcome(
        status=STATUS_FAILED,
        error_code=error_code,
        usage_source=USAGE_UNKNOWN,
        prompt_tokens=None,
        completion_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        latency_ms=latency_ms,
        content=None,
    )


def generation_transport() -> httpx.HTTPTransport:
    """固定出站 transport：不重试，也不继承环境代理与 CA。"""

    return httpx.HTTPTransport(retries=NETWORK_RETRIES, trust_env=False)


class DeepSeekAnswerGenerator:
    """用固定 endpoint 与有界响应执行一次业务生成；不重试、不读配置以外的凭据。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_response_bytes: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise GenerationConfigError("缺少可用的 LLM 密钥")
        self._model = model
        self._max_response_bytes = max_response_bytes
        self._client = httpx.Client(
            base_url=DEEPSEEK_BASE_URL,
            timeout=timeout_seconds,
            transport=transport if transport is not None else generation_transport(),
            trust_env=False,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> DeepSeekAnswerGenerator:
        """用进程配置显式构造；未开启或缺少密钥时静态失败且不联网。"""

        if not settings.llm_enabled:
            raise GenerationConfigError("业务问答生成未启用")
        if settings.llm_api_key is None:
            raise GenerationConfigError("业务问答生成未配置 LLM_API_KEY")
        return cls(
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_response_bytes=settings.llm_max_response_bytes,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def generate(
        self, messages: Sequence[ChatMessage], *, max_output_tokens: int
    ) -> GenerationOutcome:
        """发起唯一一次请求并把失败/超时/截断映射为事实，而不是异常。"""

        payload = build_chat_payload(self._model, messages, max_output_tokens=max_output_tokens)
        started = time.monotonic()
        try:
            response = self._post(payload)
        except httpx.TimeoutException:
            return GenerationOutcome(
                status=STATUS_TIMEOUT,
                error_code="TIMEOUT",
                usage_source=USAGE_UNKNOWN,
                prompt_tokens=None,
                completion_tokens=None,
                prompt_cache_hit_tokens=None,
                prompt_cache_miss_tokens=None,
                latency_ms=_elapsed_ms(started),
                content=None,
            )
        except _ProviderHTTPError as error:
            # 非 2xx 不读正文，只记录状态码；3xx（含 302）也一律失败。
            return _failure(f"HTTP_{error.status_code}", _elapsed_ms(started))
        except httpx.DecodingError:
            # httpx 解压/内容解码失败（如 gzip 响应体损坏）：字节已不可信，按无效响应落失败事实。
            # 必须放在 ``httpx.RequestError`` 之前——``DecodingError`` 是 ``RequestError`` 的子类。
            return _failure("INVALID_RESPONSE", _elapsed_ms(started))
        except UnicodeDecodeError:
            # 成功体不是合法 UTF-8：静态无效响应，不让 ``UnicodeDecodeError`` 冒泡中断记账。
            return _failure("INVALID_RESPONSE", _elapsed_ms(started))
        except httpx.RequestError:
            return _failure("NETWORK_ERROR", _elapsed_ms(started))

        latency_ms = _elapsed_ms(started)
        if response is None:
            # 成功体超过本地有界上限：不解析、不把部分字节当回答。
            return _failure("INVALID_RESPONSE", latency_ms)

        try:
            body = json.loads(response)
        except ValueError:
            return _failure("INVALID_RESPONSE", latency_ms)
        if not isinstance(body, dict):
            return _failure("INVALID_RESPONSE", latency_ms)
        return self._interpret_body(body, latency_ms)

    def _post(self, payload: dict[str, Any]) -> str | None:
        """执行一次 POST；非 2xx 抛 :class:`_ProviderHTTPError`，超限返回 None。"""

        with self._client.stream("POST", CHAT_COMPLETIONS_PATH, json=payload) as response:
            if not 200 <= response.status_code < 300:
                raise _ProviderHTTPError(response.status_code)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    return None
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="strict")

    @staticmethod
    def _interpret_body(body: dict[str, Any], latency_ms: int) -> GenerationOutcome:
        usage = _interpret_usage(body.get("usage"))
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return _failure("INVALID_RESPONSE", latency_ms)
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            # 截断是明确失败；仍然保留 provider 已报告的 usage 事实（若可用）。
            return GenerationOutcome(
                status=STATUS_FAILED,
                error_code="TRUNCATED",
                usage_source=usage.source,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
                latency_ms=latency_ms,
                content=None,
            )
        if usage.error_code is not None:
            return _failure(usage.error_code, latency_ms)
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content:
            return _failure("INVALID_RESPONSE", latency_ms)
        return GenerationOutcome(
            status=STATUS_SUCCEEDED,
            error_code=None,
            usage_source=usage.source,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
            latency_ms=latency_ms,
            content=content,
        )


__all__ = [
    "ANSWER_STAGE",
    "CHAT_COMPLETIONS_PATH",
    "DEEPSEEK_BASE_URL",
    "NETWORK_RETRIES",
    "PROVIDER",
    "STATUS_FAILED",
    "STATUS_SUCCEEDED",
    "STATUS_TIMEOUT",
    "USAGE_PROVIDER_REPORTED",
    "USAGE_UNKNOWN",
    "AnswerGenerator",
    "DeepSeekAnswerGenerator",
    "GenerationConfigError",
    "GenerationOutcome",
    "build_chat_payload",
    "generation_transport",
]
