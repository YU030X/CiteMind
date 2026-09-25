"""冻结的 index profile 编码契约：七个字段、规范 JSON 与 ``config_hash``。

本模块是 :class:`rag_backend.models.indexing.IndexProfile` 行内容的镜像：把 embedding
模型、revision、维度、归一化、tokenizer 产物摘要、切分版本与关键词分析器版本收敛成一份
不可变契约，并用规范化 JSON 的 SHA-256 作为 ``config_hash``，供插入 ``index_profile``
前去重。

规范 JSON 与 ``config_hash`` 的计算是标准库纯计算：不碰数据库、网络或文件 IO。只有
:func:`default_index_profile` 这个默认工厂会按需构造 jieba 关键词分析器
（:func:`current_keyword_analyzer_version`），因此它要求 jieba 已安装且私有临时目录可写，
失败时抛 :class:`rag_backend.retrieval.keyword_analyzer.KeywordAnalyzerError`。导入本模块
本身不初始化 jieba，也不依赖 ``tokenizers`` / ``torch`` / ``transformers``，可在 api 镜像内
安全导入。

规范化固定为 ``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False)`` 的 UTF-8 字节（无 BOM、无换行）：字段顺序与序列化顺序无关，布尔值为
JSON ``true``/``false``。``schema_version`` 单独进入 JSON 但不是 dataclass 字段。改变任一
字段、``schema_version`` 或 tokenizer/关键词身份都会改变 ``config_hash``。

tokenizer 身份在 profile 契约里重新钉死四个产物摘要，与 worker 的
:data:`rag_backend.ingestion.token_counting.TOKENIZER_ARTIFACTS` 及 inference 的
``MODEL_ARTIFACT_DIGESTS`` 由单测交叉约束；因此本模块导入期不依赖 ``tokenizers``。关键词
分析器 id 只在 :func:`current_keyword_analyzer_version` 内按需构造（构造即校验 jieba 版本
与两份词典），导入期不初始化 jieba。

本模块不建立数据库连接、不写 seed、不激活 KB、不注册任务；契约常量一旦与实际冻结模型/
切分/检索身份不符，应由静态失败暴露，而不是在运行期静默降级。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

# 契约 schema 版本；单独进入规范 JSON 但不作为 dataclass 字段。
PROFILE_SCHEMA_VERSION: Final = "index-profile-v1"

# 冻结的编码契约：模型、revision、维度与归一化。改变其中任何一项都必须新建 profile。
DEFAULT_EMBEDDING_MODEL: Final = "BAAI/bge-small-zh-v1.5"
DEFAULT_MODEL_REVISION: Final = "7999e1d3359715c523056ef9478215996d62a620"
DEFAULT_DIMENSION: Final = 512
DEFAULT_NORMALIZE: Final = True

# 切分契约版本；与 rag_backend.ingestion.chunking.CHUNKER_VERSION 一致。
DEFAULT_CHUNKER_VERSION: Final = "heading-pack-v1"

# tokenizer_revision 的构成片段；摘要由 TOKENIZER_ARTIFACTS 复算，不手写。
TOKENIZER_MODEL_SHORT_NAME: Final = "bge-small-zh-v1.5"
TOKENIZER_ARTIFACTS_DIGEST_LABEL: Final = "tokenizer-artifacts-v1-sha256"

# 固定 revision 下四个 tokenizer 产物的字节数与 SHA-256；与 worker / inference 钉死值一致。
TOKENIZER_ARTIFACTS: Final[dict[str, tuple[int, str]]] = {
    "tokenizer.json": (
        439125,
        "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    ),
    "vocab.txt": (
        109540,
        "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
    ),
    "tokenizer_config.json": (
        367,
        "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    ),
    "special_tokens_map.json": (
        125,
        "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    ),
}

_STRING_FIELDS: Final = (
    "embedding_model",
    "model_revision",
    "tokenizer_revision",
    "chunker_version",
    "keyword_analyzer_version",
)


class ProfileContractError(ValueError):
    """字段类型、取值或空值不符合冻结契约；调用方应视为编码错误而非运行期降级。"""


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    """规范 JSON 的 UTF-8 字节：排序键、紧凑分隔、不转义非 ASCII、拒绝 NaN。"""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def tokenizer_artifacts_digest(
    artifacts: Mapping[str, tuple[int, str]] = TOKENIZER_ARTIFACTS,
) -> str:
    """返回四个 tokenizer 产物 ``{filename: {size, sha256}}`` 规范 JSON 的 SHA-256。

    规范 JSON 固定为 ``sort_keys=True, separators=(",", ":"), ensure_ascii=False`` 的 UTF-8
    字节；摘要为小写 64 位十六进制。返回值稳定且与实际产物内容解耦，仅由摘要表决定。
    """

    payload: dict[str, dict[str, object]] = {
        name: {"size": size, "sha256": sha256} for name, (size, sha256) in artifacts.items()
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def tokenizer_revision(
    model_revision: str = DEFAULT_MODEL_REVISION,
    artifacts: Mapping[str, tuple[int, str]] = TOKENIZER_ARTIFACTS,
) -> str:
    """由模型 revision 与 tokenizer 产物摘要构造可复算的 v1 tokenizer 身份字符串。"""

    return (
        f"{TOKENIZER_MODEL_SHORT_NAME}@{model_revision}"
        f":{TOKENIZER_ARTIFACTS_DIGEST_LABEL}={tokenizer_artifacts_digest(artifacts)}"
    )


@lru_cache(maxsize=1)
def current_keyword_analyzer_version() -> str:
    """构造并校验版本化 jieba 分析器，返回其身份 id；进程内只构造一次。

    分析器在调用时按需导入并构造，因此导入本模块不会初始化 jieba，也不会读写共享
    ``jieba.cache``；构造成功即代表 jieba 版本与基础/领域词典摘要已按固定契约校验。

    这是本模块唯一涉及 IO 与外部依赖的入口：会读取 jieba 基础词典与包内领域词典字节，并在
    进程可写的私有临时目录内建缓存（构造结束即删除）。jieba 未安装、词典字节不符或临时目录
    不可写时抛 :class:`rag_backend.retrieval.keyword_analyzer.KeywordAnalyzerError`。
    """

    from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzer

    return KeywordAnalyzer().analyzer_id


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexProfileContract:
    """不可变默认 index profile 的七个字段；``config_hash`` 由 :meth:`config_hash` 派生。

    ``__post_init__`` 只做契约内的静态校验：字符串字段必须是非空 ``str``、``dimension``
    必须恰好是 :data:`DEFAULT_DIMENSION`、``normalize`` 必须是 ``True``。不做长度或字符集
    限制等投机校验。
    """

    embedding_model: str
    model_revision: str
    dimension: int
    normalize: bool
    tokenizer_revision: str
    chunker_version: str
    keyword_analyzer_version: str

    def __post_init__(self) -> None:
        for name in _STRING_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ProfileContractError(f"{name} 必须是非空字符串")
        if isinstance(self.dimension, bool) or not isinstance(self.dimension, int):
            raise ProfileContractError("dimension 必须是整数")
        if self.dimension != DEFAULT_DIMENSION:
            raise ProfileContractError(f"dimension 必须是 {DEFAULT_DIMENSION}")
        if not isinstance(self.normalize, bool):
            raise ProfileContractError("normalize 必须是布尔值")
        if self.normalize is not True:
            raise ProfileContractError("normalize 必须是 True")

    def canonical_bytes(self) -> bytes:
        """返回含 ``schema_version`` 的规范 JSON 的 UTF-8 字节，不含 BOM 与换行。"""

        payload: dict[str, object] = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "embedding_model": self.embedding_model,
            "model_revision": self.model_revision,
            "dimension": self.dimension,
            "normalize": self.normalize,
            "tokenizer_revision": self.tokenizer_revision,
            "chunker_version": self.chunker_version,
            "keyword_analyzer_version": self.keyword_analyzer_version,
        }
        return _canonical_json_bytes(payload)

    def config_hash(self) -> str:
        """返回 :meth:`canonical_bytes` 的 SHA-256，小写 64 位十六进制。"""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def default_index_profile() -> IndexProfileContract:
    """构造默认（唯一已冻结）profile：模型/revision/维度/归一化/切分/身份全部来自常量。

    仅此一处按需构造 jieba 分析器以取身份 id；其余字段为模块常量，tokenizer 摘要由
    :data:`TOKENIZER_ARTIFACTS` 复算。因此调用本函数会触发词典读取与私有临时目录 IO，
    jieba 缺失或临时目录不可写时抛
    :class:`rag_backend.retrieval.keyword_analyzer.KeywordAnalyzerError`。
    """

    return IndexProfileContract(
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        model_revision=DEFAULT_MODEL_REVISION,
        dimension=DEFAULT_DIMENSION,
        normalize=DEFAULT_NORMALIZE,
        tokenizer_revision=tokenizer_revision(),
        chunker_version=DEFAULT_CHUNKER_VERSION,
        keyword_analyzer_version=current_keyword_analyzer_version(),
    )
