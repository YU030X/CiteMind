"""业务 DeepSeek 客户端单测：请求契约、有界读取与失败分类，全部使用 MockTransport。

不联网、不付费、不读真实 ``.env``；真实 provider 连通性由显式 opt-in 的隔离探针负责。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from rag_backend.config import Settings
from rag_backend.generation.deepseek_client import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_TIMEOUT,
    USAGE_PROVIDER_REPORTED,
    USAGE_UNKNOWN,
    DeepSeekAnswerGenerator,
    GenerationConfigError,
    build_chat_payload,
    generation_transport,
)
from rag_backend.generation.deepseek_prompt import NON_THINKING, ChatMessage, ThinkingChoice

MESSAGES = [ChatMessage(role="user", content="问题")]
MODEL = "deepseek-flash"

SUCCESS_BODY: dict[str, Any] = {
    "choices": [
        {
            "finish_reason": "stop",
            "message": {"content": '{"sentences":[]}'},
        }
    ],
    "usage": {
        "prompt_tokens": 11,
        "completion_tokens": 3,
        "prompt_cache_hit_tokens": 5,
        "prompt_cache_miss_tokens": 6,
    },
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "llm_enabled": True,
        "llm_api_key": "test-key",
    }
    values.update(overrides)
    return Settings(**values)


def _generator(handler: Any, **overrides: Any) -> DeepSeekAnswerGenerator:
    settings = _settings(**overrides)
    return DeepSeekAnswerGenerator.from_settings(
        settings, transport=httpx.MockTransport(handler)
    )


def _generate_once(
    generator: DeepSeekAnswerGenerator, **overrides: Any
) -> Any:
    """默认以非思考 + 服务端默认模型调用；个别用例可覆盖 model / thinking。"""

    options: dict[str, Any] = {
        "model": MODEL,
        "max_output_tokens": 800,
        "thinking": NON_THINKING,
    }
    options.update(overrides)
    return generator.generate(MESSAGES, **options)


def test_build_chat_payload_disables_thinking_and_streaming() -> None:
    payload = build_chat_payload(MODEL, MESSAGES, max_output_tokens=800)

    assert payload["stream"] is False
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 800
    assert payload["model"] == MODEL
    assert payload["messages"] == [{"role": "user", "content": "问题"}]
    # 关闭思考时不发送强度字段：``reasoning_effort`` 只属于开启思考的组合。
    assert "reasoning_effort" not in payload


def test_build_chat_payload_enables_thinking_with_documented_effort() -> None:
    payload = build_chat_payload(
        MODEL,
        MESSAGES,
        max_output_tokens=800,
        thinking=ThinkingChoice(enabled=True, effort="max"),
    )

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert payload["model"] == MODEL

    # 开启但未指定强度：沿用官方默认 high，而不是省略字段（省略等于 provider 默认，仍应显式给出）。
    defaulted = build_chat_payload(
        MODEL, MESSAGES, max_output_tokens=800, thinking=ThinkingChoice(enabled=True)
    )
    assert defaulted["reasoning_effort"] == "high"


def test_generation_transport_never_retries_and_ignores_env() -> None:
    transport = generation_transport()

    assert isinstance(transport, httpx.HTTPTransport)


def test_from_settings_requires_enable_and_key() -> None:
    with pytest.raises(GenerationConfigError):
        DeepSeekAnswerGenerator.from_settings(
            _settings(llm_enabled=False, llm_api_key="k")
        )
    # enabled 但缺密钥时 Settings 本身就会拒绝启动；同时客户端也必须显式拒绝，
    # 避免任何绕过启动校验的构造路径联网。
    bypassed = Settings.model_construct(
        llm_enabled=True,
        llm_api_key=None,
        llm_model="deepseek-flash",
        llm_timeout_seconds=60.0,
        llm_max_response_bytes=4096,
    )
    with pytest.raises(GenerationConfigError):
        DeepSeekAnswerGenerator.from_settings(bypassed)


def test_settings_reject_enabling_generation_without_key() -> None:
    with pytest.raises(ValueError):
        _settings(llm_enabled=True, llm_api_key=None)


def test_success_returns_content_and_provider_usage() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=SUCCESS_BODY)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.usage_source == USAGE_PROVIDER_REPORTED
    assert outcome.content == '{"sentences":[]}'
    assert outcome.prompt_tokens == 11
    assert outcome.completion_tokens == 3
    assert outcome.prompt_cache_hit_tokens == 5
    assert outcome.prompt_cache_miss_tokens == 6
    assert outcome.error_code is None
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["body"]["stream"] is False
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["model"] == MODEL
    assert captured["authorization"] == "Bearer test-key"


def test_thinking_request_sends_enabled_switch_and_effort() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=SUCCESS_BODY)

    generator = _generator(handler)
    try:
        outcome = _generate_once(
            generator, thinking=ThinkingChoice(enabled=True, effort="low")
        )
    finally:
        generator.close()

    assert outcome.status == STATUS_SUCCEEDED
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["reasoning_effort"] == "low"


def test_reasoning_content_is_ignored_and_completion_tokens_not_double_counted() -> None:
    """思考模式返回的 CoT 不进入回答；provider 的 completion_tokens 已含 reasoning，原样记账。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["choices"] = [
            {
                "finish_reason": "stop",
                "message": {
                    "content": '{"sentences":[]}',
                    "reasoning_content": "内部思考过程，绝不外泄",
                },
            }
        ]
        body["usage"] = {
            "prompt_tokens": 11,
            "completion_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 100},
        }
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator, thinking=ThinkingChoice(enabled=True, effort="high"))
    finally:
        generator.close()

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.content == '{"sentences":[]}'
    assert "内部思考" not in (outcome.content or "")
    # 不把 reasoning_tokens 再加到 completion_tokens 上。
    assert outcome.completion_tokens == 120


def test_http_error_is_a_failed_fact_without_body_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "HTTP_500"
    assert outcome.content is None
    assert outcome.prompt_tokens is None


def test_timeout_is_reported_as_timeout_fact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_TIMEOUT
    assert outcome.error_code == "TIMEOUT"
    assert outcome.usage_source == USAGE_UNKNOWN


def test_network_error_is_a_failed_fact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "NETWORK_ERROR"


def test_missing_usage_is_not_reported_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "MISSING_USAGE"
    assert outcome.usage_source == USAGE_UNKNOWN
    assert outcome.prompt_tokens is None
    assert outcome.content is None


def test_length_truncation_is_an_explicit_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["choices"] = [
            {"finish_reason": "length", "message": {"content": "半截"}}
        ]
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "TRUNCATED"
    assert outcome.content is None
    # provider 已报告的真实 token 仍然保留，不伪装成本地估算。
    assert outcome.prompt_tokens == 11
    assert outcome.completion_tokens == 3


def test_thinking_mode_exhausted_by_reasoning_reports_truncation() -> None:
    """思考与回答共用 ``max_tokens``：被推理耗尽时如实落 ``TRUNCATED``，不抬高预算。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["choices"] = [
            {"finish_reason": "length", "message": {"content": "", "reasoning_content": "…"}}
        ]
        body["usage"] = {
            "prompt_tokens": 11,
            "completion_tokens": 800,
            "completion_tokens_details": {"reasoning_tokens": 800},
        }
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator, thinking=ThinkingChoice(enabled=True, effort="max"))
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "TRUNCATED"
    assert outcome.content is None
    assert outcome.completion_tokens == 800


def test_oversized_response_body_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["choices"] = [
            {"finish_reason": "stop", "message": {"content": "x" * 4096}}
        ]
        return httpx.Response(200, json=body)

    generator = _generator(handler, llm_max_response_bytes=128)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_RESPONSE"


def test_non_json_success_body_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_RESPONSE"


def test_non_utf8_success_body_is_a_failed_fact() -> None:
    """成功体不是合法 UTF-8：静态失败，不让 UnicodeDecodeError 冒泡中断记账。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\xfa")

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_RESPONSE"
    assert outcome.usage_source == USAGE_UNKNOWN
    assert outcome.prompt_tokens is None
    assert outcome.content is None


def test_bad_compression_is_a_failed_fact() -> None:
    """httpx 解压失败（DecodingError 是 RequestError 子类）也必须落静态失败事实。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=b"not gzip at all"
        )

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_RESPONSE"
    assert outcome.content is None


@pytest.mark.parametrize(
    "bad_completion_tokens",
    [True, False, -1, 1.5, "3"],
)
def test_invalid_completion_usage_token_is_rejected(bad_completion_tokens: Any) -> None:
    """bool、负数、浮点与非整数都不是可信 provider usage，必须整份判失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["usage"] = {
            "prompt_tokens": 11,
            "completion_tokens": bad_completion_tokens,
        }
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_USAGE"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None
    assert outcome.content is None


def test_non_integer_prompt_usage_token_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        # ``True`` 是 int 的子类，必须被 bool 分支先拦截。
        body["usage"] = {"prompt_tokens": True, "completion_tokens": 3}
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_USAGE"


def test_non_dict_usage_is_missing_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["usage"] = [11, 3]
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "MISSING_USAGE"
    assert outcome.usage_source == USAGE_UNKNOWN
    assert outcome.prompt_tokens is None


@pytest.mark.parametrize("choices", [None, [], ["not-a-dict"]])
def test_missing_or_malformed_choices_is_invalid_response(choices: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(SUCCESS_BODY)
        body["choices"] = choices
        return httpx.Response(200, json=body)

    generator = _generator(handler)
    try:
        outcome = _generate_once(generator)
    finally:
        generator.close()

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "INVALID_RESPONSE"
    assert outcome.content is None
