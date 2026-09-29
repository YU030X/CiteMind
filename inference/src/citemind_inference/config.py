"""inference 进程的启动配置。

token 使用 ``SecretStr`` 保存，``repr``/日志不会打印明文。生产环境使用开发占位值
时在启动阶段直接失败，并明确指出需要替换的变量。embedding 相关配置全部有显式上限，
并在启动时做交叉校验：矛盾的组合（例如单条 token 上限高于单请求总预算）会使启动失败，
而不是在运行期静默降级。

编码契约是冻结的：模型、revision、维度与模型自身的最大输入长度都不可在运行期更改。
"""

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

# 旧版本曾用 CITEMIND_ 前缀；重命名为裸名后，任何残留旧键都必须在读取任何 source 之前
# 被发现，避免静默读到旧值或误以为新配置已经生效。这里只检查键名，不读取也不回显值。
LEGACY_ENV_PREFIX = "CITEMIND_"


def reject_legacy_prefixed_env_vars(*env_vars_sources: Mapping[str, str | None]) -> None:
    """进程环境与 dotenv 中任意 CITEMIND_* 旧键都让启动显式失败（只读键名，不回显值）。"""

    offenders = sorted(
        {
            key.upper()
            for env_vars in env_vars_sources
            for key in env_vars
            if key.upper().startswith(LEGACY_ENV_PREFIX)
        }
    )
    if offenders:
        raise ValueError(
            "检测到已废弃的 CITEMIND_ 前缀环境变量，请去掉前缀改为裸名后重新启动: "
            + "、".join(offenders)
        )


# 开发占位 token：仅用于本地与测试。生产环境使用该值会在启动校验中失败。
DEVELOPMENT_INFERENCE_TOKEN = "citemind-inference"

# 冻结的编码契约：模型、revision、维度与模型自身的最大输入长度。改变其中任何一项都必须
# 新建 index profile；这些值不是运行期可随意调整的参数。
FROZEN_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
FROZEN_EMBEDDING_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
EMBEDDING_DIMENSION = 512
EMBEDDING_MAX_TOKENS = 512

# 查询编码契约：BGE 中文模型的官方检索 instruction 前缀。前缀是独立的具名契约，不并入
# modelRevision——仅凭 revision 无法识别 instruction 漂移。前缀只由服务端在编码前恰好追加
# 一次，客户端只发送原始查询文本；用户输入本身以该文本开头时仍原样不改、不去重。改变前缀、
# 追加位置或计数方式都必须新建版本并重新评估，不能静默切换。
BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
QUERY_ENCODING_CONTRACT = "bge-zh-query-v1"

# 冻结的 rerank 契约：模型与 revision 不可在运行期更改；模型结构由本地 config 决定。
# 权重是否完整由构建期产物清单与运行期身份校验共同约束，不在源码里写未经下载核验的 SHA-256。
FROZEN_RERANK_MODEL = "BAAI/bge-reranker-base"
FROZEN_RERANK_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
# bge-reranker-base 的位置上限；pair 输入按 max_length 截断，不拒绝超长候选。
RERANK_MAX_TOKENS = 512

# 容器内的默认本地模型目录；镜像需要提前把冻结 revision 的权重放到这里。
DEFAULT_EMBEDDING_MODEL_PATH = Path("/models/bge-small-zh-v1.5")
DEFAULT_RERANK_MODEL_PATH = Path("/models/bge-reranker-base")

# 文本字节预算默认 256 KiB。原始请求体预算默认由文本预算推导：按 JSON 转义常见放大
# 量级取 3 倍再加固定余量覆盖键名、引号与逗号。该推导不是所有 JSON 转义的最坏界
# （例如把短 ASCII 文本逐字写成 \uXXXX 可放大到约 6 倍），这类极端转义会先被解析前
# 的 body 字节上限拒绝；需要独立调整传输预算时显式设置
# EMBEDDING_MAX_REQUEST_BYTES。
DEFAULT_MAX_TEXT_BYTES = 262144
REQUEST_BYTES_MULTIPLIER = 3
REQUEST_BYTES_OVERHEAD = 1024


def derived_request_byte_limit(total_text_bytes: int) -> int:
    """由文本字节预算推导原始请求体上限，使调紧文本预算时传输预算随之收紧。"""

    return REQUEST_BYTES_MULTIPLIER * total_text_bytes + REQUEST_BYTES_OVERHEAD


class Settings(BaseSettings):
    """inference 进程配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        # 校验失败时隐藏输入值，避免 ValidationError 打印 token 明文。
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    # 内部接口的 Bearer token。默认空值只用于让“环境变量缺失”与“显式空值”走同一条
    # 启动失败路径；真正的值必须由 INFERENCE_TOKEN 注入。
    inference_token: SecretStr = SecretStr("")

    # 本地模型目录：必须已包含冻结 revision 的权重，运行期不会联网下载。
    embedding_model_path: Path = DEFAULT_EMBEDDING_MODEL_PATH
    embedding_model_revision: str = FROZEN_EMBEDDING_REVISION

    # 单次请求的条数上限。
    embedding_max_batch_size: int = 16
    # 单次请求中所有文本解析后合计的 UTF-8 字节上限（语义预算）。
    embedding_max_total_bytes: int = DEFAULT_MAX_TEXT_BYTES
    # 单次请求原始请求体的字节上限（传输预算）。在 JSON 解析前按实际收到的字节数生效，
    # 因此额外字段、空白与转义都无法绕过。留空时根据文本预算自动推导，所以调紧文本预算
    # 会同时收紧传输预算；显式设置时必须不小于文本预算。
    embedding_max_request_bytes: int | None = None
    # 单条文本的字符数上限。
    embedding_max_chars_per_text: int = 8000
    # 单条文本编码后的 token 上限（含特殊 token）；不得超过模型自身的 512。
    embedding_max_tokens_per_text: int = EMBEDDING_MAX_TOKENS
    # 单次请求的 token 位置预算，按 padding 后的真实占用计算：条数 × 批内最长 token 数。
    embedding_max_total_tokens: int = 8192

    # 同时执行的编码批次数；单模型 CPU 服务默认 1，避免多个 tokenizer/前向抢占核心。
    embedding_max_concurrency: int = 1
    # 并发许可之外的等待队列深度；队列满时立即返回可重试的 503，而不是无限排队。
    embedding_queue_depth: int = 4
    # 等待并发许可的最长时间，超时返回可重试的 503；必须是有限正数。
    embedding_queue_wait_seconds: Annotated[
        float, Field(gt=0.0, allow_inf_nan=False)
    ] = 5.0
    # torch 的 CPU 线程数；启动时固定，避免每个请求各自抢占线程。
    embedding_torch_threads: int = 2

    # rerank 默认关闭：关闭时不加载权重、/capabilities 报 rerank.ready=false，
    # /internal/rerank 仍受 Bearer 与请求体上限保护并返回静态 503 RERANK_NOT_READY。
    rerank_enabled: bool = False
    rerank_model_path: Path = DEFAULT_RERANK_MODEL_PATH
    rerank_model_revision: str = FROZEN_RERANK_REVISION
    # 单次请求的候选上限；query 与单个候选各自还有字符上限，合计受字节预算约束。
    rerank_max_candidates: int = 10
    rerank_max_query_chars: int = 4000
    rerank_max_text_chars: int = 8000
    rerank_max_total_bytes: int = DEFAULT_MAX_TEXT_BYTES
    # 原始请求体上限；留空时由候选文本预算推导（与 embedding 同一推导规则）。
    rerank_max_request_bytes: int | None = None
    # rerank 的 CPU 并发与排队；单模型 CPU 服务默认 1，队列满时立即返回可重试的 503。
    rerank_max_concurrency: int = 1
    rerank_queue_depth: int = 4
    rerank_queue_wait_seconds: Annotated[
        float, Field(gt=0.0, allow_inf_nan=False)
    ] = 5.0

    @property
    def request_byte_limit(self) -> int:
        """实际生效的原始请求体字节上限（显式配置优先，否则由文本预算推导）。"""

        if self.embedding_max_request_bytes is not None:
            return self.embedding_max_request_bytes
        return derived_request_byte_limit(self.embedding_max_total_bytes)

    @property
    def rerank_request_byte_limit(self) -> int:
        """实际生效的 rerank 原始请求体字节上限（显式配置优先，否则由文本预算推导）。"""

        if self.rerank_max_request_bytes is not None:
            return self.rerank_max_request_bytes
        return derived_request_byte_limit(self.rerank_max_total_bytes)

    @field_validator("embedding_model_revision")
    @classmethod
    def validate_model_revision(cls, revision: str) -> str:
        if revision != FROZEN_EMBEDDING_REVISION:
            raise ValueError(
                "EMBEDDING_MODEL_REVISION 只接受冻结 revision "
                f"{FROZEN_EMBEDDING_REVISION}；更换模型或 revision 必须另建 index profile"
            )
        return revision

    @field_validator("rerank_model_revision")
    @classmethod
    def validate_rerank_revision(cls, revision: str) -> str:
        if revision != FROZEN_RERANK_REVISION:
            raise ValueError(
                "RERANK_MODEL_REVISION 只接受冻结 revision "
                f"{FROZEN_RERANK_REVISION}；更换模型或 revision 必须同步更新构建期资产与文档"
            )
        return revision

    @model_validator(mode="after")
    def validate_token(self) -> "Settings":
        token = self.inference_token.get_secret_value()

        if not token:
            raise ValueError("INFERENCE_TOKEN 不能为空")
        if self.environment == "production" and token == DEVELOPMENT_INFERENCE_TOKEN:
            raise ValueError(
                "生产环境不得使用开发占位 INFERENCE_TOKEN；请注入独立密钥"
            )
        return self

    @model_validator(mode="after")
    def validate_embedding_limits(self) -> "Settings":
        positive_limits = {
            "EMBEDDING_MAX_BATCH_SIZE": self.embedding_max_batch_size,
            "EMBEDDING_MAX_TOTAL_BYTES": self.embedding_max_total_bytes,
            "EMBEDDING_MAX_CHARS_PER_TEXT": self.embedding_max_chars_per_text,
            "EMBEDDING_MAX_TOKENS_PER_TEXT": self.embedding_max_tokens_per_text,
            "EMBEDDING_MAX_TOTAL_TOKENS": self.embedding_max_total_tokens,
            "EMBEDDING_MAX_CONCURRENCY": self.embedding_max_concurrency,
            "EMBEDDING_TORCH_THREADS": self.embedding_torch_threads,
        }
        for name, value in positive_limits.items():
            if value < 1:
                raise ValueError(f"{name} 必须 >= 1")
        # 0 表示“不排队，满了立即拒绝”，是合法配置。
        if self.embedding_queue_depth < 0:
            raise ValueError("EMBEDDING_QUEUE_DEPTH 必须 >= 0")

        # 模型自身只支持 512 个位置；配置再大也无法在不截断的前提下编码。
        if self.embedding_max_tokens_per_text > EMBEDDING_MAX_TOKENS:
            raise ValueError(
                f"EMBEDDING_MAX_TOKENS_PER_TEXT 不得超过模型上限 {EMBEDDING_MAX_TOKENS}"
            )
        # UTF-8 每个字符至少占 1 字节；字符上限大于字节上限时可编码的文本永远通不过字节检查。
        if self.embedding_max_chars_per_text > self.embedding_max_total_bytes:
            raise ValueError(
                "EMBEDDING_MAX_CHARS_PER_TEXT 不得超过 "
                "EMBEDDING_MAX_TOTAL_BYTES"
            )
        # 一条文本的 token 预算必须装得进单请求总预算，否则单独把上限调高没有意义。
        if self.embedding_max_total_tokens < self.embedding_max_tokens_per_text:
            raise ValueError(
                "EMBEDDING_MAX_TOTAL_TOKENS 不得小于 "
                "EMBEDDING_MAX_TOKENS_PER_TEXT"
            )
        # 传输预算至少要能装下语义预算，否则合法请求必然先在字节层被拒。
        if self.embedding_max_request_bytes is not None and (
            self.embedding_max_request_bytes < self.embedding_max_total_bytes
        ):
            raise ValueError(
                "EMBEDDING_MAX_REQUEST_BYTES 不得小于 "
                "EMBEDDING_MAX_TOTAL_BYTES"
            )
        return self

    @model_validator(mode="after")
    def validate_rerank_limits(self) -> "Settings":
        positive_limits = {
            "RERANK_MAX_CANDIDATES": self.rerank_max_candidates,
            "RERANK_MAX_QUERY_CHARS": self.rerank_max_query_chars,
            "RERANK_MAX_TEXT_CHARS": self.rerank_max_text_chars,
            "RERANK_MAX_TOTAL_BYTES": self.rerank_max_total_bytes,
            "RERANK_MAX_CONCURRENCY": self.rerank_max_concurrency,
        }
        for name, value in positive_limits.items():
            if value < 1:
                raise ValueError(f"{name} 必须 >= 1")
        # 0 表示“不排队，满了立即拒绝”，与 embedding 队列语义一致。
        if self.rerank_queue_depth < 0:
            raise ValueError("RERANK_QUEUE_DEPTH 必须 >= 0")
        # UTF-8 每个字符至少占 1 字节；字符上限大于字节上限时合法文本永远通不过字节检查。
        if self.rerank_max_text_chars > self.rerank_max_total_bytes:
            raise ValueError("RERANK_MAX_TEXT_CHARS 不得超过 RERANK_MAX_TOTAL_BYTES")
        if self.rerank_max_query_chars > self.rerank_max_total_bytes:
            raise ValueError("RERANK_MAX_QUERY_CHARS 不得超过 RERANK_MAX_TOTAL_BYTES")
        if self.rerank_max_request_bytes is not None and (
            self.rerank_max_request_bytes < self.rerank_max_total_bytes
        ):
            raise ValueError(
                "RERANK_MAX_REQUEST_BYTES 不得小于 RERANK_MAX_TOTAL_BYTES"
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """在读取字段前拒绝旧 CITEMIND_* 键；只查键名，不读取或回显值。"""

        reject_legacy_prefixed_env_vars(
            *[
                source.env_vars
                for source in (env_settings, dotenv_settings)
                if isinstance(source, EnvSettingsSource)
            ]
        )
        return init_settings, env_settings, dotenv_settings, file_secret_settings


@lru_cache
def get_settings() -> Settings:
    return Settings()
