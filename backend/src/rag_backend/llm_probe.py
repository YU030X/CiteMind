"""一次性真实云 LLM 探针：核对 DeepSeek 连通性与用量账本。

这是显式 opt-in 的运维入口，不是业务路径：不注册 API 路由、不自动运行、不做业务
调用。只有同时满足 ``ALLOW_LLM_PROBE=1`` 与独立非占位 ``LLM_API_KEY``
时才向固定 HTTPS endpoint 发一次请求；缺失任一项时快速失败，且不联网、不写账本。
在调用 provider 之前先对目标库做预检：连接角色必须是 citemind_api、``public.llm_usage``
必须存在且必要列可读、SELECT+INSERT 权限可用；预检失败直接非零退出，不触网、不写账本。
一次 provider attempt 恰好写一行 ``llm_usage`` 事实，失败与超时也追加；provider
调用成功但账本写入或回读失败时明确非零退出，不把连通性报告成验收成功。
provider 调用与 PostgreSQL 提交无法原子化：预检通过后数据库仍可能故障，此时网络已发生
但事实可能未落账；“一次 attempt 恰好一行”只在账本可用时成立，失败必须非零且如实报告。
出站只使用固定 DeepSeek HTTPS endpoint，且显式不继承 ``HTTP(S)_PROXY``、``SSL_CERT_FILE``
与 ``SSL_CERT_DIR``；宿主必须能直连外网，只能经代理的网络会以连接失败非零结束。
prompt、响应正文与密钥都不写入数据库、日志或 marker，响应内容也不参与任何功能判断。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from rag_backend.config import Settings, get_settings

PROVIDER = "deepseek"
# 供应商权威文档指定的生产 endpoint；不暴露为配置项，避免以任意 base_url 伪装真实验收。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CHAT_COMPLETIONS_PATH = "/chat/completions"
PROBE_STAGE = "llm_probe"
PROBE_ATTEMPT = 1
# 固定 prompt：不含文档或用户输入，也不进入数据库与日志。
PROBE_PROMPT = "Reply with the single word: ok"
# 输出与超时都保持极小；不重试，避免放大器级费用。
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 15.0
# 目标库连接上限：库不可达时必须尽快非零退出，不能无期限挂起。
PROBE_DB_CONNECT_TIMEOUT_SECONDS = 5
NETWORK_RETRIES = 0

EXIT_OK = 0
EXIT_PRECONDITION = 2
EXIT_LEDGER_FAILURE = 3
EXIT_PROVIDER_FAILURE = 4
EXIT_VERIFICATION_FAILURE = 5
EXIT_PREFLIGHT_FAILURE = 6

# 账本只接受 citemind_api 运行角色；其他 DSN（含 worker/迁移账号）在预检阶段明确拒绝。
API_DB_ROLE = "citemind_api"
# 预检要求的必要列；与迁移 20260923_0004 的列集合保持同步。
LLM_USAGE_REQUIRED_COLUMNS = (
    "id",
    "provider",
    "model",
    "stage",
    "status",
    "error_code",
    "usage_source",
    "attempt",
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "latency_ms",
)

CURRENT_USER_SQL = text("SELECT current_user")
TABLE_PRESENT_SQL = text("SELECT to_regclass('public.llm_usage')")
TABLE_PRIVILEGE_SQL = text("SELECT has_table_privilege('public.llm_usage', :privilege)")
TABLE_COLUMNS_SQL = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'llm_usage'"
)
# 预检实际读取必要列（LIMIT 0 不取数据），直接证明这些列可 SELECT。
TABLE_COLUMN_READ_SQL = text(
    "SELECT " + ", ".join(LLM_USAGE_REQUIRED_COLUMNS) + " FROM llm_usage LIMIT 0"
)

# 明显的占位值不视为可用密钥；与 .env.example 中的说明保持同步。
PLACEHOLDER_API_KEYS = frozenset(
    {"changeme", "placeholder", "your-api-key", "sk-your-key", "citemind", "citemind-llm"}
)

INSERT_USAGE_SQL = text(
    "INSERT INTO llm_usage ("
    " id, provider, model, stage, status, error_code, usage_source, attempt,"
    " prompt_tokens, completion_tokens, prompt_cache_hit_tokens,"
    " prompt_cache_miss_tokens, latency_ms"
    ") VALUES ("
    " :id, :provider, :model, :stage, :status, :error_code, :usage_source, :attempt,"
    " :prompt_tokens, :completion_tokens, :prompt_cache_hit_tokens,"
    " :prompt_cache_miss_tokens, :latency_ms"
    ")"
)

SELECT_USAGE_SQL = text(
    "SELECT id, status, usage_source, prompt_tokens, completion_tokens "
    "FROM llm_usage WHERE id = :id"
)


@dataclass(frozen=True)
class ProbeOutcome:
    """一次 provider attempt 的最终事实，尚未落库。"""

    status: str
    usage_source: str
    error_code: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    latency_ms: int


@dataclass(frozen=True)
class PersistedUsage:
    """从账本回读的一行事实。"""

    id: uuid.UUID
    status: str
    usage_source: str
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True)
class ProbeReport:
    """CLI 结果：退出码与不含敏感内容的单行消息。"""

    exit_code: int
    message: str
    usage_id: uuid.UUID | None = None


class LedgerPreflightError(RuntimeError):
    """账本预检未通过：迁移缺失、连接角色不符或权限不足。"""


class UsageLedger(Protocol):
    """预检、追加并回读用量事实的最小接口，便于单元测试注入。"""

    def preflight(self) -> None: ...

    def append(self, outcome: ProbeOutcome, *, model: str) -> uuid.UUID: ...

    def read(self, usage_id: uuid.UUID) -> PersistedUsage: ...

    def close(self) -> None: ...


def resolve_probe_api_key(settings: Settings) -> str | None:
    """返回可用的探针密钥；未配置或为占位值返回 None。"""

    if settings.llm_api_key is None:
        return None
    value = settings.llm_api_key.get_secret_value().strip()
    if not value or value.lower() in PLACEHOLDER_API_KEYS:
        return None
    return value


def build_request_payload(model: str) -> dict[str, Any]:
    """固定探针请求；显式关闭 thinking，非流式，并限制输出长度。

    未添加 ``reasoning_effort``：它与 ``thinking: {"type": "disabled"}`` 的兼容性
    尚未由官方文档确认，需一次真实计费调用核实后再决定，避免凭猜测发送未知字段。
    默认 thinking 必须显式关闭，不能依赖供应商默认值。
    """

    return {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": PROBE_MAX_TOKENS,
    }


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


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


def interpret_success_body(
    body: Any,
) -> tuple[str, str, int | None, int | None, int | None, int | None, str | None]:
    """从 2xx 响应体提取 provider usage；缺失或非法时按失败事实处理，不伪造 token。

    任何非法数值（含缓存 token 负数或 bool）都使整行判为 ``INVALID_USAGE``，四个 token
    字段全部为 NULL，避免数据库非负 CHECK 在收费后才拒收。
    """

    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return "FAILED", "UNKNOWN", None, None, None, None, "MISSING_USAGE"

    prompt_state, prompt_tokens = _classify_usage_token(usage.get("prompt_tokens"))
    completion_state, completion_tokens = _classify_usage_token(usage.get("completion_tokens"))
    hit_state, hit_tokens = _classify_usage_token(usage.get("prompt_cache_hit_tokens"))
    miss_state, miss_tokens = _classify_usage_token(usage.get("prompt_cache_miss_tokens"))

    if _TOKEN_INVALID in (prompt_state, completion_state, hit_state, miss_state):
        return "FAILED", "UNKNOWN", None, None, None, None, "INVALID_USAGE"
    if prompt_state == _TOKEN_ABSENT or completion_state == _TOKEN_ABSENT:
        return "FAILED", "UNKNOWN", None, None, None, None, "MISSING_USAGE"

    return (
        "SUCCEEDED",
        "PROVIDER_REPORTED",
        prompt_tokens,
        completion_tokens,
        hit_tokens,
        miss_tokens,
        None,
    )


def probe_transport() -> httpx.HTTPTransport:
    """固定探针出站 transport：不重试，且不继承环境代理与 CA。

    httpx 的 ``HTTPTransport`` 默认 ``trust_env=True``，会读取 ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR`` 作为出站信任根；显式关闭后仍连接固定 HTTPS endpoint，
    但不受宿主环境变量改变信任路径。
    """

    return httpx.HTTPTransport(retries=NETWORK_RETRIES, trust_env=False)


def probe_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """固定探针客户端；``trust_env=False`` 防止 HTTP(S)_PROXY 与 CA 环境变量生效。

    即使传入了显式 transport（httpx 此时本就不构建环境代理 mount），也显式关闭，
    作为对未来 httpx 行为变化的防回归。
    """

    return httpx.Client(
        base_url=DEEPSEEK_BASE_URL,
        timeout=PROBE_TIMEOUT_SECONDS,
        transport=transport if transport is not None else probe_transport(),
        trust_env=False,
    )


def call_provider(settings: Settings, transport: httpx.BaseTransport | None = None) -> ProbeOutcome:
    """发起唯一一次 provider 请求；失败/超时映射为事实而不是异常。"""

    key = resolve_probe_api_key(settings)
    if key is None:
        raise ValueError("call_provider 需要已解析的探针密钥")
    payload = build_request_payload(settings.llm_model)

    started = time.monotonic()
    try:
        with probe_client(transport) as client:
            response = client.post(
                CHAT_COMPLETIONS_PATH,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.TimeoutException:
        return _failure("TIMEOUT", "TIMEOUT", started)
    except httpx.RequestError:
        return _failure("FAILED", "NETWORK_ERROR", started)

    latency_ms = _elapsed_ms(started)
    if not 200 <= response.status_code < 300:
        # 只把 2xx 当候选成功；3xx（含 302）与其他非 2xx 一律 FAILED，退非零。
        # 不读取错误正文，避免把 provider 回显内容带进数据库或日志。
        return ProbeOutcome(
            status="FAILED",
            usage_source="UNKNOWN",
            error_code=f"HTTP_{response.status_code}",
            prompt_tokens=None,
            completion_tokens=None,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            latency_ms=latency_ms,
        )

    try:
        body = response.json()
    except ValueError:
        return ProbeOutcome(
            status="FAILED",
            usage_source="UNKNOWN",
            error_code="INVALID_RESPONSE",
            prompt_tokens=None,
            completion_tokens=None,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            latency_ms=latency_ms,
        )

    (
        status,
        usage_source,
        prompt_tokens,
        completion_tokens,
        cache_hit_tokens,
        cache_miss_tokens,
        error_code,
    ) = interpret_success_body(body)
    return ProbeOutcome(
        status=status,
        usage_source=usage_source,
        error_code=error_code,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_cache_hit_tokens=cache_hit_tokens,
        prompt_cache_miss_tokens=cache_miss_tokens,
        latency_ms=latency_ms,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _failure(status: str, error_code: str, started: float) -> ProbeOutcome:
    return ProbeOutcome(
        status=status,
        usage_source="UNKNOWN",
        error_code=error_code,
        prompt_tokens=None,
        completion_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        latency_ms=_elapsed_ms(started),
    )


class SqlAlchemyUsageLedger:
    """用 api 运行角色 DSN 预检、追加并回读 llm_usage；无常驻连接。"""

    def __init__(self, database_url: str) -> None:
        self._engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            # 预检连接必须有上限；否则目标库不可达时会一直等待而不是明确失败。
            connect_args={"connect_timeout": PROBE_DB_CONNECT_TIMEOUT_SECONDS},
        )

    def preflight(self) -> None:
        """在触网前核对目标库：角色、迁移与表列、SELECT+INSERT 权限。

        任何一项不满足都抛 ``LedgerPreflightError``；本方法不写库、不触网。
        预检通过也不能保证后续提交成功：云调用与事务提交无法原子化。
        """

        try:
            with self._engine.connect() as connection:
                role = str(connection.scalar(CURRENT_USER_SQL))
                if role != API_DB_ROLE:
                    raise LedgerPreflightError(
                        f"探针账本必须使用 {API_DB_ROLE} 角色，当前连接是 {role}；"
                        "拒绝非 api DSN，未发起网络请求"
                    )
                if connection.scalar(TABLE_PRESENT_SQL) is None:
                    raise LedgerPreflightError(
                        "目标库没有 public.llm_usage；请先对目标库执行迁移后再试，"
                        "未发起网络请求"
                    )
                for privilege in ("SELECT", "INSERT"):
                    if not connection.scalar(TABLE_PRIVILEGE_SQL, {"privilege": privilege}):
                        raise LedgerPreflightError(
                            f"{API_DB_ROLE} 缺少 public.llm_usage 的 {privilege} 权限；"
                            "未发起网络请求"
                        )
                columns = {str(name) for name in connection.scalars(TABLE_COLUMNS_SQL)}
                missing = sorted(set(LLM_USAGE_REQUIRED_COLUMNS) - columns)
                if missing:
                    raise LedgerPreflightError(
                        "public.llm_usage 缺少必要列：" + "、".join(missing) + "；未发起网络请求"
                    )
                # 表级 SELECT 权限已核对，再用 LIMIT 0 实际读取必要列确认可查询。
                connection.execute(TABLE_COLUMN_READ_SQL)
        except LedgerPreflightError:
            raise
        except SQLAlchemyError as error:
            raise LedgerPreflightError(
                f"账本预检无法连接目标库（{type(error).__name__}）；未发起网络请求"
            ) from error

    def append(self, outcome: ProbeOutcome, *, model: str) -> uuid.UUID:
        usage_id = uuid.uuid4()
        with self._engine.begin() as connection:
            connection.execute(
                INSERT_USAGE_SQL,
                {
                    "id": usage_id,
                    "provider": PROVIDER,
                    "model": model,
                    "stage": PROBE_STAGE,
                    "status": outcome.status,
                    "error_code": outcome.error_code,
                    "usage_source": outcome.usage_source,
                    "attempt": PROBE_ATTEMPT,
                    "prompt_tokens": outcome.prompt_tokens,
                    "completion_tokens": outcome.completion_tokens,
                    "prompt_cache_hit_tokens": outcome.prompt_cache_hit_tokens,
                    "prompt_cache_miss_tokens": outcome.prompt_cache_miss_tokens,
                    "latency_ms": outcome.latency_ms,
                },
            )
        return usage_id

    def read(self, usage_id: uuid.UUID) -> PersistedUsage:
        with self._engine.connect() as connection:
            row = connection.execute(SELECT_USAGE_SQL, {"id": usage_id}).one()
        return PersistedUsage(
            id=row[0],
            status=str(row[1]),
            usage_source=str(row[2]),
            prompt_tokens=_optional_int(row[3]),
            completion_tokens=_optional_int(row[4]),
        )

    def close(self) -> None:
        self._engine.dispose()


def run_probe(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    ledger: UsageLedger | None = None,
) -> ProbeReport:
    """执行探针并返回退出码；opt-in 或密钥缺失时不触网、不建账本。"""

    if not settings.allow_llm_probe:
        return ProbeReport(
            EXIT_PRECONDITION,
            "未设置 ALLOW_LLM_PROBE=1，未发起网络请求，也未写账本",
        )
    if resolve_probe_api_key(settings) is None:
        return ProbeReport(
            EXIT_PRECONDITION,
            "未配置可用的 LLM_API_KEY（或仍是占位值），未发起网络请求，也未写账本",
        )

    owned_ledger = ledger is None
    resolved_ledger = ledger if ledger is not None else SqlAlchemyUsageLedger(settings.database_url)
    try:
        try:
            resolved_ledger.preflight()
        except LedgerPreflightError as error:
            # 预检在触网之前：未迁移/无库/角色或权限不符时零网络、零账本。
            return ProbeReport(
                EXIT_PREFLIGHT_FAILURE,
                f"账本预检失败：{error}",
            )
        outcome = call_provider(settings, transport)
        try:
            usage_id = resolved_ledger.append(outcome, model=settings.llm_model)
        except Exception as error:
            # provider 调用已经发生，但事实不能落库：必须失败，绝不报告连通验收成功。
            return ProbeReport(
                EXIT_LEDGER_FAILURE,
                f"provider 调用已发生但账本写入失败（{type(error).__name__}），未核对连通性",
            )
        try:
            persisted = resolved_ledger.read(usage_id)
        except Exception as error:
            return ProbeReport(
                EXIT_LEDGER_FAILURE,
                f"账本已写入但回读失败（{type(error).__name__}），未核对连通性",
                usage_id=usage_id,
            )
    finally:
        if owned_ledger:
            resolved_ledger.close()

    if persisted.status != outcome.status or persisted.usage_source != outcome.usage_source:
        return ProbeReport(
            EXIT_VERIFICATION_FAILURE,
            f"账本事实与本次调用不一致：usageId={persisted.id}，未通过核对",
            usage_id=persisted.id,
        )

    if outcome.status != "SUCCEEDED":
        return ProbeReport(
            EXIT_PROVIDER_FAILURE,
            f"llm-probe {outcome.status}: usageId={persisted.id} "
            f"errorCode={outcome.error_code} latencyMs={outcome.latency_ms}",
            usage_id=persisted.id,
        )

    prompt_tokens = persisted.prompt_tokens
    completion_tokens = persisted.completion_tokens
    if (
        persisted.usage_source != "PROVIDER_REPORTED"
        or prompt_tokens is None
        or completion_tokens is None
        or prompt_tokens + completion_tokens <= 0
    ):
        return ProbeReport(
            EXIT_VERIFICATION_FAILURE,
            f"provider 报告 usage 未通过核对：usageId={persisted.id}，未视为连通成功",
            usage_id=persisted.id,
        )

    return ProbeReport(
        EXIT_OK,
        f"llm-probe 成功: usageId={persisted.id} provider={PROVIDER} "
        f"model={settings.llm_model} promptTokens={prompt_tokens} "
        f"completionTokens={completion_tokens} latencyMs={outcome.latency_ms}",
        usage_id=persisted.id,
    )


def main() -> int:
    """命令行入口：``python -m rag_backend.llm_probe``。"""

    report = run_probe(get_settings())
    print(report.message)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
