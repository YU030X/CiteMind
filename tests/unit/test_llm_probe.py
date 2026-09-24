"""云 LLM 探针的纯逻辑测试：用 httpx.MockTransport 注入，绝不发出真实请求。"""

import ssl
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from rag_backend.config import Settings
from rag_backend.llm_probe import (
    DEEPSEEK_BASE_URL,
    EXIT_LEDGER_FAILURE,
    EXIT_OK,
    EXIT_PRECONDITION,
    EXIT_PREFLIGHT_FAILURE,
    EXIT_PROVIDER_FAILURE,
    EXIT_VERIFICATION_FAILURE,
    NETWORK_RETRIES,
    PROBE_DB_CONNECT_TIMEOUT_SECONDS,
    PROBE_MAX_TOKENS,
    PROBE_PROMPT,
    LedgerPreflightError,
    PersistedUsage,
    ProbeOutcome,
    SqlAlchemyUsageLedger,
    build_request_payload,
    call_provider,
    interpret_success_body,
    probe_client,
    probe_transport,
    resolve_probe_api_key,
    run_probe,
)

SECRET_KEY = "sk-unit-test-secret-value"

SUCCESS_BODY: dict[str, Any] = {
    "id": "completion-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {
        "prompt_tokens": 9,
        "completion_tokens": 1,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 9,
    },
}


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def probe_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"allow_llm_probe": True, "llm_api_key": SECRET_KEY}
    values.update(overrides)
    return settings(**values)


def mock_transport(handler: Any) -> httpx.BaseTransport:
    return httpx.MockTransport(handler)


class FakeLedger:
    """内存账本，记录调用顺序并允许注入预检/追加/回读失败。"""

    def __init__(
        self,
        *,
        fail_preflight: bool = False,
        fail_append: bool = False,
        fail_read: bool = False,
        override: PersistedUsage | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.appended: list[tuple[ProbeOutcome, str]] = []
        self.fail_preflight = fail_preflight
        self.fail_append = fail_append
        self.fail_read = fail_read
        self.override = override
        self.closed = False
        self.preflight_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1
        self.events.append("preflight")
        if self.fail_preflight:
            raise LedgerPreflightError("目标库没有 public.llm_usage")

    def append(self, outcome: ProbeOutcome, *, model: str) -> Any:
        self.events.append("append")
        if self.fail_append:
            raise RuntimeError("db down")
        self.appended.append((outcome, model))
        return "00000000-0000-0000-0000-0000000000aa"

    def read(self, usage_id: Any) -> PersistedUsage:
        self.events.append("read")
        if self.fail_read:
            raise RuntimeError("db down")
        if self.override is not None:
            return self.override
        outcome, _ = self.appended[-1]
        return PersistedUsage(
            id=usage_id,
            status=outcome.status,
            usage_source=outcome.usage_source,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
        )

    def close(self) -> None:
        self.closed = True


def test_build_request_payload_disables_thinking_and_limits_output() -> None:
    payload = build_request_payload("deepseek-flash")

    assert payload["model"] == "deepseek-flash"
    assert payload["stream"] is False
    assert payload["thinking"] == {"type": "disabled"}
    # 官方文档未确认 reasoning_effort 与 thinking:disabled 兼容，暂不发送该字段。
    assert "reasoning_effort" not in payload
    assert payload["max_tokens"] == PROBE_MAX_TOKENS
    assert payload["messages"] == [{"role": "user", "content": PROBE_PROMPT}]


def test_resolve_probe_api_key_rejects_missing_blank_and_placeholder() -> None:
    assert resolve_probe_api_key(settings()) is None
    assert resolve_probe_api_key(settings(llm_api_key="   ")) is None
    assert resolve_probe_api_key(settings(llm_api_key="changeme")) is None
    assert resolve_probe_api_key(settings(llm_api_key=SECRET_KEY)) == SECRET_KEY


def test_settings_does_not_render_the_llm_key() -> None:
    rendered = repr(probe_settings())

    assert SECRET_KEY not in rendered


def test_settings_defaults_to_probe_disabled(monkeypatch: Any) -> None:
    monkeypatch.delenv("ALLOW_LLM_PROBE", raising=False)

    assert settings().allow_llm_probe is False
    assert settings().llm_api_key is None


def test_settings_parses_probe_opt_in_and_key(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALLOW_LLM_PROBE", "1")
    monkeypatch.setenv("LLM_API_KEY", SECRET_KEY)
    resolved = settings()

    assert resolved.allow_llm_probe is True
    assert resolve_probe_api_key(resolved) == SECRET_KEY


def test_ai_gateway_dev_key_does_not_enter_product_settings(monkeypatch: Any) -> None:
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "dev-only-jev-key")
    resolved = settings()

    # 开发期 Node Jev 的密钥不是本配置字段，不会进入产品配置。
    assert resolved.llm_api_key is None
    assert "dev-only-jev-key" not in repr(resolved)


def test_run_probe_without_opt_in_is_offline_and_does_not_touch_the_ledger() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger()
    report = run_probe(settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PRECONDITION
    assert calls == 0
    assert ledger.appended == []
    assert ledger.preflight_calls == 0
    assert "未发起网络请求" in report.message


def test_run_probe_without_key_is_offline_and_does_not_touch_the_ledger() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger()
    report = run_probe(
        settings(allow_llm_probe=True), transport=mock_transport(handler), ledger=ledger
    )

    assert report.exit_code == EXIT_PRECONDITION
    assert calls == 0
    assert ledger.appended == []
    assert ledger.preflight_calls == 0


def test_run_probe_rejects_placeholder_key_before_networking() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger()
    report = run_probe(
        settings(allow_llm_probe=True, llm_api_key="placeholder"),
        transport=mock_transport(handler),
        ledger=ledger,
    )

    assert report.exit_code == EXIT_PRECONDITION
    assert calls == 0
    assert ledger.appended == []
    assert ledger.preflight_calls == 0


def test_run_probe_preflight_runs_before_network_and_appends_once() -> None:
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("network")
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger(events=events)
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_OK
    assert ledger.preflight_calls == 1
    assert events == ["preflight", "network", "append", "read"]


def test_run_probe_preflight_failure_is_offline_and_non_zero() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger(fail_preflight=True)
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PREFLIGHT_FAILURE
    assert calls == 0
    assert ledger.appended == []
    assert ledger.preflight_calls == 1
    assert "预检失败" in report.message


def test_run_probe_success_appends_and_verifies_provider_usage() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_OK
    assert report.usage_id is not None
    assert len(ledger.appended) == 1
    outcome, model = ledger.appended[0]
    assert model == "deepseek-flash"
    assert outcome.status == "SUCCEEDED"
    assert outcome.usage_source == "PROVIDER_REPORTED"
    assert outcome.prompt_tokens == 9
    assert outcome.completion_tokens == 1
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == f"{DEEPSEEK_BASE_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    # 输出与账本事实都不包含 prompt、响应正文或密钥。
    assert PROBE_PROMPT not in report.message
    assert SECRET_KEY not in report.message
    assert "promptTokens=9" in report.message
    assert "completionTokens=1" in report.message


def test_run_probe_missing_usage_is_a_failed_fact_not_fabricated_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": []})

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "MISSING_USAGE"
    assert outcome.usage_source == "UNKNOWN"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None


def test_run_probe_http_error_records_code_without_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "HTTP_401"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None


def test_run_probe_redirect_is_failed_and_records_http_302() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, json=SUCCESS_BODY)

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    assert len(ledger.appended) == 1
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "HTTP_302"
    assert outcome.usage_source == "UNKNOWN"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None
    assert outcome.prompt_cache_hit_tokens is None
    assert outcome.prompt_cache_miss_tokens is None


def test_run_probe_negative_prompt_tokens_is_invalid_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": -1, "completion_tokens": 2}}
        )

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    assert len(ledger.appended) == 1
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "INVALID_USAGE"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None


def test_run_probe_negative_cache_tokens_is_invalid_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "prompt_cache_hit_tokens": -3,
                    "prompt_cache_miss_tokens": 5,
                }
            },
        )

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "INVALID_USAGE"
    # 负数缓存 token 不得落库，四个 token 字段全为 NULL，不靠数据库 CHECK 报错。
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None
    assert outcome.prompt_cache_hit_tokens is None
    assert outcome.prompt_cache_miss_tokens is None


def test_run_probe_boolean_token_is_invalid_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": True, "completion_tokens": 2}}
        )

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "INVALID_USAGE"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None


def test_run_probe_timeout_is_recorded_as_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    outcome, _ = ledger.appended[0]
    assert outcome.status == "TIMEOUT"
    assert outcome.error_code == "TIMEOUT"
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None


def test_run_probe_network_error_is_recorded_as_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_PROVIDER_FAILURE
    outcome, _ = ledger.appended[0]
    assert outcome.status == "FAILED"
    assert outcome.error_code == "NETWORK_ERROR"


def test_run_probe_ledger_write_failure_is_non_zero_and_never_reports_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger(fail_append=True)
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_LEDGER_FAILURE
    assert "账本写入失败" in report.message


def test_run_probe_readback_mismatch_fails_verification() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUCCESS_BODY)

    ledger = FakeLedger(
        override=PersistedUsage(
            id=UUID("00000000-0000-0000-0000-0000000000aa"),
            status="FAILED",
            usage_source="UNKNOWN",
            prompt_tokens=None,
            completion_tokens=None,
        )
    )
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_VERIFICATION_FAILURE
    assert "不一致" in report.message


def test_run_probe_zero_usage_fails_verification() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 0,
                }
            },
        )

    ledger = FakeLedger()
    report = run_probe(probe_settings(), transport=mock_transport(handler), ledger=ledger)

    assert report.exit_code == EXIT_VERIFICATION_FAILURE
    assert "usage 未通过核对" in report.message


def test_interpret_success_body_requires_integer_tokens() -> None:
    status, source, prompt, completion, _, _, error_code = interpret_success_body(
        {"usage": {"prompt_tokens": 3, "completion_tokens": "4"}}
    )

    assert status == "FAILED"
    assert source == "UNKNOWN"
    assert prompt is None
    assert completion is None
    assert error_code == "INVALID_USAGE"


def test_interpret_success_body_treats_missing_cache_tokens_as_null() -> None:
    status, source, prompt, completion, hit, miss, error_code = interpret_success_body(
        {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    )

    assert status == "SUCCEEDED"
    assert source == "PROVIDER_REPORTED"
    assert (prompt, completion) == (3, 2)
    assert (hit, miss) == (None, None)
    assert error_code is None


def test_interpret_success_body_rejects_negative_cache_tokens() -> None:
    status, source, prompt, completion, hit, miss, error_code = interpret_success_body(
        {
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "prompt_cache_miss_tokens": -1,
            }
        }
    )

    assert status == "FAILED"
    assert source == "UNKNOWN"
    assert (prompt, completion, hit, miss) == (None, None, None, None)
    assert error_code == "INVALID_USAGE"


def test_call_provider_uses_fixed_endpoint_and_zero_retries() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SUCCESS_BODY)

    outcome = call_provider(probe_settings(), mock_transport(handler))

    assert outcome.status == "SUCCEEDED"
    assert len(seen) == 1
    assert str(seen[0].url) == f"{DEEPSEEK_BASE_URL}/chat/completions"
    # 固定 retries=0；探针不做网络重试，避免费用放大。
    assert NETWORK_RETRIES == 0


def test_probe_ledger_engine_sets_a_bounded_connect_timeout(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_create_engine(database_url: str, **kwargs: Any) -> Any:
        captured["database_url"] = database_url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("rag_backend.llm_probe.create_engine", fake_create_engine)
    SqlAlchemyUsageLedger("postgresql+psycopg://citemind_api:secret@127.0.0.1:55432/citemind")

    assert PROBE_DB_CONNECT_TIMEOUT_SECONDS > 0
    assert captured["connect_args"] == {"connect_timeout": PROBE_DB_CONNECT_TIMEOUT_SECONDS}


def test_probe_transport_ignores_env_ca_even_when_it_is_malicious(
    monkeypatch: Any, tmp_path: Path
) -> None:
    bogus_ca = tmp_path / "malicious-ca.pem"
    bogus_ca.write_text("not a certificate", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(bogus_ca))

    # 对照：默认 trust_env=True 会读取 SSL_CERT_FILE，恶意 CA 使构造直接失败。
    with pytest.raises(ssl.SSLError):
        httpx.HTTPTransport(retries=0, trust_env=True)

    # 探针 transport 显式 trust_env=False，环境 CA 不参与出站信任路径。
    transport = probe_transport()
    assert isinstance(transport, httpx.HTTPTransport)
    transport.close()


def test_probe_client_does_not_build_environment_proxy_mounts(monkeypatch: Any) -> None:
    malicious_proxy = "http://malicious.proxy.invalid:8080"
    monkeypatch.setenv("HTTP_PROXY", malicious_proxy)
    monkeypatch.setenv("HTTPS_PROXY", malicious_proxy)
    monkeypatch.setenv("ALL_PROXY", malicious_proxy)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=SUCCESS_BODY))
    with probe_client(transport) as client:
        assert client.trust_env is False
        # 显式 transport 下 httpx 不构建环境代理 mount；断言它们确实为空。
        assert client._mounts == {}


def test_call_provider_ignores_env_proxy_with_injected_transport(monkeypatch: Any) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://malicious.proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://malicious.proxy.invalid:8080")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SUCCESS_BODY)

    outcome = call_provider(probe_settings(), httpx.MockTransport(handler))

    # 请求直接到达注入的 transport 的固定 DeepSeek URL，未被环境代理改写。
    assert outcome.status == "SUCCEEDED"
    assert len(seen) == 1
    assert str(seen[0].url) == f"{DEEPSEEK_BASE_URL}/chat/completions"
