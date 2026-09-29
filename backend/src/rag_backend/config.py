import ipaddress
import math
import uuid
from collections.abc import Mapping
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from rag_backend.generation.capabilities import SUPPORTED_MODEL_IDS
from rag_backend.web_hosts import parse_allowed_web_hosts

# 与 deploy/compose/compose.yml 默认暴露的本机端口一致；用户名是运行时 api 角色，
# 密码是开发占位值，生产环境会被下面的校验拒绝。
DEFAULT_DATABASE_URL = "postgresql+psycopg://citemind_api:citemind@127.0.0.1:55432/citemind"

# Redis broker 只接受 redis/rediss scheme；密码是开发占位值，生产环境会被下面的校验拒绝。
REDIS_SCHEMES = ("redis", "rediss")
DEFAULT_REDIS_PASSWORD = "citemind"

# MVP 单组织：账号、会话与 KB 成员都归属这个服务端配置的组织，客户端不能提交它。
DEFAULT_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# CSRF 令牌由该服务端密钥与会话令牌派生，因此数据库只存 hash 时 GET /me 仍能恢复同一令牌。
# 这是本地开发占位值；生产环境会被下面的校验拒绝。
DEFAULT_CSRF_SECRET = "development-csrf-secret"
MIN_PRODUCTION_CSRF_SECRET_LENGTH = 32

# 状态变更请求的 Origin 白名单；默认只覆盖本地网关的回环来源。
DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:58080,http://localhost:58080"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

DEFAULT_SESSION_COOKIE_NAME = "citemind_session"
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
DEFAULT_LOGIN_RATE_LIMIT_PER_IP = 20
DEFAULT_LOGIN_RATE_LIMIT_PER_USERNAME = 10
DEFAULT_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300

# 只有一个受信代理（网关）才能设置这个单值头；网关必须用 $remote_addr 覆盖它。
DEFAULT_CLIENT_IP_HEADER = "X-Real-IP"

# 内部 inference 的受限基址与默认超时；客户端只接受该 host 或回环测试地址。
DEFAULT_INFERENCE_BASE_URL = "http://inference:9000"
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0
# rerank 是可选增强：默认关闭；开启时单次请求超时更短，失败整体降级为原 RRF 顺序。
DEFAULT_RERANK_TIMEOUT_SECONDS = 3.0

# 旧版所有项目自有变量都带 CITEMIND_ 前缀；重命名为裸名后，任何残留旧键都必须在启动时
# 显式失败，避免静默读到旧值或误以为新配置已生效。这里只检查键名，不读取也不回显值。
LEGACY_ENV_PREFIX = "CITEMIND_"


def reject_legacy_prefixed_env_vars(*env_vars_sources: Mapping[str, str | None]) -> None:
    """进程环境与 dotenv 中任意 CITEMIND_* 旧键都让启动显式失败。"""

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
            + ", ".join(offenders)
        )


def normalise_origin(origin: str) -> str:
    """把 origin 规范化为 ``scheme://host[:port]``；无效时抛 ValueError。"""

    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"来源必须是带 host 的 http(s) origin: {origin}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"来源不能包含 path、query 或 fragment: {origin}")
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_allowed_origins(value: str) -> list[str]:
    """解析逗号分隔的 Origin 白名单并规范化为去重后的有序列表。"""

    origins: list[str] = []
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        normalised = normalise_origin(candidate)
        if normalised not in origins:
            origins.append(normalised)
    if not origins:
        raise ValueError("allowed_origins 不能为空")
    return origins


def parse_trusted_proxy_networks(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析逗号分隔的可信代理 CIDR；空值表示不信任任何代理。"""

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError as error:
            raise ValueError(f"trusted_proxy_cidrs 含无效 CIDR: {candidate}") from error
    return tuple(networks)


def validate_redis_url(redis_url: str) -> SplitResult:
    """校验 Redis broker URL 的通用规则；无效时抛 ValueError。

    这里只做启动时可独立判断的校核，不建立连接；连接失败由 Celery 启动或派发显式报错。
    """

    try:
        parsed = urlsplit(redis_url)
    except ValueError as error:
        raise ValueError("Redis URL 不是有效的 URL") from error

    if parsed.scheme not in REDIS_SCHEMES:
        raise ValueError("Redis URL 必须使用 redis 或 rediss scheme")
    if not parsed.hostname:
        raise ValueError("Redis URL 必须包含非空 host")
    return parsed


def validate_database_url(database_url: str) -> URL:
    """校验数据库 URL 的通用规则；无效时抛 ValueError。

    迁移入口也复用这里的规则，避免迁移 DSN 与应用运行配置的校验产生偏差。
    """

    try:
        url = make_url(database_url)
    except ArgumentError as error:
        raise ValueError("数据库 URL 不是有效的 SQLAlchemy URL") from error

    if url.drivername != "postgresql+psycopg":
        raise ValueError("数据库 URL 必须使用 postgresql+psycopg 驱动")
    if not url.host:
        raise ValueError("数据库 URL 必须包含非空 host")
    if not url.database:
        raise ValueError("数据库 URL 必须包含非空 database")
    return url


class Settings(BaseSettings):
    """API 进程与一次性云 LLM 探针共享的启动配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
    )

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
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    app_name: str = "CiteMind API"
    environment: Literal["development", "test", "production"] = "development"
    # 两个 DSN 含连接凭据，诊断用的 repr/str 只保留字段名，屏蔽明文；字段原值、
    # model_dump() 与连接逻辑不变（安全任务 #118）。
    database_url: Annotated[str, Field(repr=False)] = DEFAULT_DATABASE_URL
    database_echo: bool = False
    # Redis 供两类用途：worker 的 Celery broker，以及 API 的登录限流。API 未配置时
    # 登录会返回 503（fail closed），而不是回退到进程内计数；生产环境必须配置。
    redis_url: Annotated[str | None, Field(repr=False)] = None
    # 仅当显式设置时，worker 才把 probe 执行结果写成受信目录下的诊断 marker；
    # 默认 None 表示纯回显，不产生文件副作用。路径由该目录与 Celery task id 推导，
    # payload 不能控制。
    probe_marker_directory: str | None = None
    # 上传原文件的私有存储根；Compose 中由 api 专用命名卷挂载。相对路径只由服务端
    # KB ID 与内容 SHA-256 派生，不拼接用户文件名，也不作为静态资源对外暴露。
    document_storage_directory: str = "/var/lib/citemind/documents"
    # queue-probe 一次性验收入口等待 marker 的时限；只用于 Linux Compose 验收。
    queue_probe_timeout_seconds: float = 60.0
    # 是否在 API 进程 lifespan 启动后台 outbox dispatcher。非 Compose 默认关闭，
    # 避免开发/测试进程意外连 broker 或数据库写表；Compose 显式设为 true。
    dispatcher_enabled: bool = False
    # 产品专用 DeepSeek 密钥；仅由显式 opt-in 的一次性探针读取，默认不配置。
    # 与开发期 Node Jev 专用的 AI_GATEWAY_API_KEY 无关，后者不是本配置字段，
    # 不会进入本配置或 api/worker 运行时。
    llm_api_key: SecretStr | None = None
    # 一次性真实探针必须显式开启；默认关闭，避免启动或测试自动发起收费调用。
    allow_llm_probe: bool = False
    # 云生成模型名保持配置化；默认值来自供应商文档，实际可用性由真实探针核对。
    llm_model: str = "deepseek-flash"
    # 问答生成的初始 token 预算：输入目标由 api 侧本地 tokenizer 估算执行，输出上限交给 provider
    # 的 max_tokens 精确强制。两者都是初值，需在开发集上再评估；本配置不发起任何 provider 调用。
    llm_input_token_budget: int = 4000
    llm_output_token_budget: int = 800
    # 业务问答生成总开关：默认关闭，关闭时问答端点静态失败且绝不联网。开启时必须配置
    # LLM_API_KEY（启动期可独立判断）。
    llm_enabled: bool = False
    # 业务生成的单次 provider 超时与有界响应体上限；超时返回 TIMEOUT 事实而不是重试。
    llm_timeout_seconds: float = 60.0
    llm_max_response_bytes: int = 262144
    # 内部 embedding 服务的受限基址与单请求超时；worker 侧受限客户端与 api 侧检索/问答的查询
    # 编码客户端都会读取它们。三项都不加 CITEMIND_ 前缀。非 Compose 直接使用 Settings 时可不
    # 配置 token，进程仍能正常启动；deploy/compose/compose.yml 的 inference、worker 与 api
    # 三个服务都按既有插值把 INFERENCE_TOKEN 声明为必填，因此 Compose 启动前必须提供该变量。
    # 无论哪种方式，只有真正构造受限客户端时才会因 token 缺失或不合法而 failfast。
    inference_base_url: str = DEFAULT_INFERENCE_BASE_URL
    inference_timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS
    inference_token: SecretStr | None = None
    # 可降级 reranker：默认关闭。关闭时检索完全不构造/调用重排客户端，也不标记降级；
    # 开启时必须配置 INFERENCE_TOKEN（启动期可独立判断）。
    rerank_enabled: bool = False
    rerank_timeout_seconds: float = DEFAULT_RERANK_TIMEOUT_SECONDS
    # 是否在 worker 里执行真实入库（解析/切分/编码/发布）。默认 False，保持既有安全接收壳
    # 行为；显式开启前不加载模型资产、不连 inference。开启时必须配置 INFERENCE_TOKEN。
    ingest_processing_enabled: bool = False

    # 受限静态网页抓取的允许主机列表；逗号分隔的精确规范化 host（IDNA 小写、去尾点），
    # 不做后缀/通配符匹配。默认空字符串即功能禁用（fail closed），任何网页导入都静态失败。
    # 只有 API 读取它；worker 只解析已保存的 HTML blob，不需要抓取配置。
    web_fetch_allowed_hosts: str = ""

    # 单组织标识；来自服务端配置，客户端请求体与查询参数都不能覆盖它。
    organization_id: uuid.UUID = DEFAULT_ORGANIZATION_ID
    # 会话 Cookie 只携带高熵随机原令牌；数据库只保存其 hash。
    session_cookie_name: str = DEFAULT_SESSION_COOKIE_NAME
    # 生产环境默认 Secure，只能在非生产的回环来源上显式关闭。
    session_cookie_secure: bool = True
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    # 服务端 CSRF 密钥；/me 用它从会话令牌派生同一个 CSRF 令牌。
    csrf_secret: SecretStr = SecretStr(DEFAULT_CSRF_SECRET)
    # 逗号分隔的状态变更请求 Origin 白名单。
    allowed_origins: str = DEFAULT_ALLOWED_ORIGINS
    # 可信反向代理 CIDR；只有直连对端落在这些网段内时才接受 client_ip_header。
    # 空值表示不信任任何代理，限流退回直连对端地址；生产环境必须显式配置。
    trusted_proxy_cidrs: str = ""
    # 网关必须用 $remote_addr 覆盖这个单值头，应用只接受单个合法 IP，不读 X-Forwarded-For。
    client_ip_header: str = DEFAULT_CLIENT_IP_HEADER
    # 登录限流在 Redis 中原子计数，跨 API 进程共享；Redis 故障时拒绝登录。
    login_rate_limit_per_ip: int = DEFAULT_LOGIN_RATE_LIMIT_PER_IP
    login_rate_limit_per_username: int = DEFAULT_LOGIN_RATE_LIMIT_PER_USERNAME
    login_rate_limit_window_seconds: int = DEFAULT_LOGIN_RATE_LIMIT_WINDOW_SECONDS

    @property
    def allowed_origin_set(self) -> frozenset[str]:
        """规范化后的 Origin 白名单；每次请求比较时即时解析。"""

        return frozenset(parse_allowed_origins(self.allowed_origins))

    @property
    def trusted_proxy_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """可信代理网段；空元组表示不信任任何代理。"""

        return parse_trusted_proxy_networks(self.trusted_proxy_cidrs)

    @property
    def web_fetch_allowed_host_set(self) -> frozenset[str]:
        """规范化后的网页抓取允许主机集合；空集合表示功能禁用。"""

        return parse_allowed_web_hosts(self.web_fetch_allowed_hosts)

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        url = validate_database_url(self.database_url)

        if self.environment == "production":
            if not url.username:
                raise ValueError("生产环境数据库 URL 必须包含非空 username")
            if not url.password:
                raise ValueError("生产环境数据库 URL 必须包含非空 password")
            if url.password == "citemind":
                raise ValueError("生产环境不得使用默认数据库凭据")
            if self.database_echo:
                raise ValueError("生产环境不得开启 database_echo")

        if self.redis_url is not None:
            redis_url = validate_redis_url(self.redis_url)
            if self.environment == "production":
                if not redis_url.password:
                    raise ValueError("生产环境 Redis URL 必须包含非空 password")
                if redis_url.password == DEFAULT_REDIS_PASSWORD:
                    raise ValueError("生产环境不得使用默认 Redis 凭据")

        if self.probe_marker_directory is not None and not self.probe_marker_directory:
            raise ValueError("probe_marker_directory 不能为空字符串；应留空或提供受信目录")
        if not self.document_storage_directory.strip():
            raise ValueError("document_storage_directory 不能为空字符串")
        if self.queue_probe_timeout_seconds <= 0:
            raise ValueError("queue_probe_timeout_seconds 必须为正数")
        if self.dispatcher_enabled and self.redis_url is None:
            raise ValueError("启用 dispatcher 必须配置 Redis URL 作为投递 broker")
        if self.llm_api_key is not None and not self.llm_api_key.get_secret_value().strip():
            # 空白值等于未配置密钥，避免探针把空字符串当成凭据。
            self.llm_api_key = None
        if not self.llm_model.strip():
            raise ValueError("llm_model 不能为空字符串")
        if self.llm_model not in SUPPORTED_MODEL_IDS:
            # 未经验证 tokenizer/渲染契约的模型不得作为服务端默认：启动期即可独立判断。
            raise ValueError("llm_model 必须属于服务端已验证的模型白名单")
        if self.llm_input_token_budget <= 0:
            raise ValueError("llm_input_token_budget 必须为正数")
        if self.llm_output_token_budget <= 0:
            raise ValueError("llm_output_token_budget 必须为正数")
        if (
            not math.isfinite(self.llm_timeout_seconds)
            or self.llm_timeout_seconds <= 0
        ):
            raise ValueError("llm_timeout_seconds 必须是有限正数")
        if self.llm_max_response_bytes <= 0:
            raise ValueError("llm_max_response_bytes 必须为正数")
        if self.llm_enabled and self.llm_api_key is None:
            # 业务生成必须能向固定 endpoint 发请求；缺失密钥属启动期可独立判断的无效配置。
            raise ValueError("开启 llm_enabled 必须配置 LLM_API_KEY")
        if not self.inference_base_url.strip():
            raise ValueError("inference_base_url 不能为空字符串")
        if (
            not math.isfinite(self.inference_timeout_seconds)
            or self.inference_timeout_seconds <= 0
        ):
            raise ValueError("inference_timeout_seconds 必须是有限正数")
        if self.inference_token is not None and not self.inference_token.get_secret_value().strip():
            # 空白值等于未配置；API 仍能启动，客户端构造时才失败。
            self.inference_token = None
        if self.ingest_processing_enabled and self.inference_token is None:
            # 真实处理必须能构造受限编码客户端；缺失 token 属启动期可独立判断的无效配置。
            raise ValueError("开启 ingest_processing_enabled 必须配置 INFERENCE_TOKEN")
        if (
            not math.isfinite(self.rerank_timeout_seconds)
            or self.rerank_timeout_seconds <= 0
        ):
            raise ValueError("rerank_timeout_seconds 必须是有限正数")
        if self.rerank_enabled and self.inference_token is None:
            # rerank 客户端同样必须能携带 Bearer token；缺失属启动期可独立判断的无效配置。
            raise ValueError("开启 rerank_enabled 必须配置 INFERENCE_TOKEN")

        try:
            # 允许主机列表在启动期即可独立判断；非法条目（含通配符/端口/scheme）直接失败。
            parse_allowed_web_hosts(self.web_fetch_allowed_hosts)
        except ValueError as error:
            raise ValueError(f"web_fetch_allowed_hosts 含非法主机名: {error}") from error

        origins = parse_allowed_origins(self.allowed_origins)
        trusted_proxies = parse_trusted_proxy_networks(self.trusted_proxy_cidrs)
        if not self.session_cookie_name.strip():
            raise ValueError("session_cookie_name 不能为空字符串")
        if not self.client_ip_header.strip():
            raise ValueError("client_ip_header 不能为空字符串")
        if not all(character.isalnum() or character == "-" for character in self.client_ip_header):
            raise ValueError("client_ip_header 必须是合法的 HTTP 头名")
        if self.session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds 必须为正数")
        if self.login_rate_limit_per_ip <= 0:
            raise ValueError("login_rate_limit_per_ip 必须为正数")
        if self.login_rate_limit_per_username <= 0:
            raise ValueError("login_rate_limit_per_username 必须为正数")
        if self.login_rate_limit_window_seconds <= 0:
            raise ValueError("login_rate_limit_window_seconds 必须为正数")

        csrf_secret = self.csrf_secret.get_secret_value()
        if not csrf_secret.strip():
            raise ValueError("csrf_secret 不能为空字符串")

        if self.environment == "production":
            if not self.session_cookie_secure:
                raise ValueError("生产环境必须启用 Secure 会话 Cookie")
            if self.redis_url is None:
                raise ValueError("生产环境必须配置 Redis URL 以支持登录限流")
            if not trusted_proxies:
                raise ValueError(
                    "生产环境必须显式配置 TRUSTED_PROXY_CIDRS（可信网关 CIDR），"
                    "否则每-IP 限流会退化为按对端地址计数"
                )
            # 只拒绝真正的全网段（0.0.0.0/0、::/0）：信任它会接受任意客户端伪造的
            # client_ip_header，等于取消对端校验；更窄的宽网段仍由运维按网络拓扑判断。
            for network in trusted_proxies:
                if network.prefixlen == 0:
                    raise ValueError(
                        "生产环境 TRUSTED_PROXY_CIDRS 不得包含 0.0.0.0/0 或 ::/0 全网段"
                    )
            if (
                csrf_secret == DEFAULT_CSRF_SECRET
                or len(csrf_secret) < MIN_PRODUCTION_CSRF_SECRET_LENGTH
            ):
                raise ValueError("生产环境 csrf_secret 必须是独立的至少 32 字符密钥")
            if any(urlsplit(origin).scheme != "https" for origin in origins):
                raise ValueError("生产环境 allowed_origins 必须全部使用 https")
        elif not self.session_cookie_secure:
            for origin in origins:
                if urlsplit(origin).hostname not in LOOPBACK_HOSTS:
                    raise ValueError(
                        "禁用 Secure 会话 Cookie 时 allowed_origins 必须仅限回环地址"
                    )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
