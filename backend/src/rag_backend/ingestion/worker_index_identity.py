"""worker 索引身份工厂：显式构造本进程的 index profile 并核验四件 tokenizer 资产。

本模块是 worker 专用前置能力：``rag_backend.ingestion`` 包在导入期不引用它，API 镜像
import ``rag_backend.ingestion`` 因此不会加载 ``tokenizers``；只有上游显式 import 本模块时
才需要 worker 组依赖。当前安全接收壳允许没有 ``/models`` 的宿主以 solo 方式消费消息；若本片
在 worker 启动信令里无条件挂载资产校验，该宿主会启动即失败，且 Linux prefork 下启动失败会
反复 fork/崩溃。因此这里**只**提供显式工厂 :func:`initialize_worker_index_identity`，不注册
signal、不写 marker、不读 DB、不 ACK 消息；是否接线由后续切片单独决定并验收。

成功时工厂返回 :class:`WorkerIndexIdentity`：只读 :class:`IndexProfileContract`、真实
``parser_version``、已校验的 :class:`LocalTokenizerCounter` 与
:class:`KeywordAnalyzer`。校验顺序固定：

1. 先构造 ``LocalTokenizerCounter(model_directory)``：核对四个 tokenizer 产物的大小与
   SHA-256、文件集合，并只调用一次 ``Tokenizer.from_file``；
2. 再核对 :data:`rag_backend.ingestion.token_counting.TOKENIZER_ARTIFACTS` 与
   :data:`rag_backend.models.profile_contract.TOKENIZER_ARTIFACTS` 逐件相等；由于第 1 步
   已把真实字节绑定到前者，逐件相等即可传递地认为 ``profile_contract`` 的摘要描述真实
   资产，**不再二次哈希**磁盘产物；
3. 用冻结常量构造 ``IndexProfileContract``，并核对 ``chunking.CHUNKER_VERSION``、
   ``embedding_client.EXPECTED_MODEL_REVISION`` 与 ``embedding_client.EMBEDDING_DIMENSION``
   一致；``tokenizer_revision`` 由冻结 revision 与摘要标签复算；``keyword_analyzer_version``
   取本进程只构造一次的 ``KeywordAnalyzer().analyzer_id``，不调用 ``default_index_profile()``
   额外构造分析器。

这里只核验 worker 的四件 tokenizer 字节；embedding 权重和 inference 实例实际使用的模型
revision 均未读取或核验。模型 revision 的跨源检查仅比较代码常量。

失败（tokenizer 资产、摘要表跨源不一致、revision/维度/切分器不一致、jieba 版本或词典不符、
契约构造失败）统一收敛为静态 :class:`WorkerIndexIdentityError`，消息只含类别，不回显目录、
DSN 或原始异常链。工厂用 ``@lru_cache(maxsize=1)`` 按 ``Path`` 参数缓存**仅成功**结果；
失败不缓存，修复后可重试。``.cache_clear()`` 只供测试使用，本模块不提供生产 reset 接口。
生产路径不硬编码 ``config_hash``；golden ``4af4…d57fa`` 只在单测里复算。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from rag_backend.ingestion import token_counting
from rag_backend.ingestion.token_counting import (
    DEFAULT_MODEL_DIRECTORY,
    LocalTokenizerCounter,
    TokenCounterError,
)
from rag_backend.models import profile_contract
from rag_backend.models.profile_contract import (
    IndexProfileContract,
    ProfileContractError,
)
from rag_backend.retrieval.keyword_analyzer import (
    KeywordAnalyzer,
    KeywordAnalyzerError,
)

# 静态失败原因；所有分支只输出这些固定文本，绝不拼接路径、DSN、UUID 或原始异常。
_TOKENIZER_ASSET_FAILURE: Final = "worker tokenizer 资产校验失败"
_ARTIFACT_TABLE_MISMATCH: Final = "tokenizer 资产摘要表跨源不一致"
_REVISION_MISMATCH: Final = "embedding 模型 revision 跨源不一致"
_DIMENSION_MISMATCH: Final = "embedding 维度跨源不一致"
_CHUNKER_MISMATCH: Final = "切分器版本跨源不一致"
_TOKENIZER_REVISION_UNBOUND: Final = "tokenizer_revision 未绑定冻结 revision 或摘要标签"
_ANALYZER_FAILURE: Final = "关键词分析器身份校验失败"
_CONTRACT_FAILURE: Final = "index profile 契约构造失败"


class WorkerIndexIdentityError(RuntimeError):
    """worker 索引身份初始化失败的统一静态错误类别；调用方应转为启动失败。"""


@dataclass(frozen=True, slots=True)
class WorkerIndexIdentity:
    """worker 启动期一次构造、进程内长期复用的只读索引身份。

    ``profile`` 是不可变契约；``token_counter`` 与 ``keyword_analyzer`` 是已校验并复用的
    实例，调用方不得替换或重建。``parser_version`` 与 profile 分离，因为解析器升级不要求
    新建 profile。
    """

    profile: IndexProfileContract
    parser_version: str
    pdf_parser_version: str
    docx_parser_version: str
    token_counter: LocalTokenizerCounter
    keyword_analyzer: KeywordAnalyzer


def _resolve_tokenizer_revision(frozen_revision: str) -> str:
    """用冻结 revision 与冻结摘要表复算 ``tokenizer_revision``，不重新读取磁盘产物。"""

    revision = profile_contract.tokenizer_revision(
        frozen_revision, profile_contract.TOKENIZER_ARTIFACTS
    )
    expected_prefix = f"{profile_contract.TOKENIZER_MODEL_SHORT_NAME}@{frozen_revision}:"
    if not revision.startswith(expected_prefix) or (
        profile_contract.TOKENIZER_ARTIFACTS_DIGEST_LABEL not in revision
    ):
        raise WorkerIndexIdentityError(_TOKENIZER_REVISION_UNBOUND)
    return revision


def _verify_cross_source_constants() -> str:
    """核对 worker / 契约 / 编码客户端的冻结常量一致，返回冻结模型 revision。"""

    # 逐件相等即可传递地把第 1 步的真实字节校验绑定到 profile 契约摘要，无需二次哈希。
    if dict(token_counting.TOKENIZER_ARTIFACTS) != dict(profile_contract.TOKENIZER_ARTIFACTS):
        raise WorkerIndexIdentityError(_ARTIFACT_TABLE_MISMATCH)

    # 这些模块只在 worker 启动期按需导入，避免模块导入期拉入 markdown_it / sqlalchemy。
    from rag_backend.ingestion import chunking, embedding_client

    frozen_revision = profile_contract.DEFAULT_MODEL_REVISION
    if frozen_revision != embedding_client.EXPECTED_MODEL_REVISION:
        raise WorkerIndexIdentityError(_REVISION_MISMATCH)
    if profile_contract.DEFAULT_DIMENSION != embedding_client.EMBEDDING_DIMENSION:
        raise WorkerIndexIdentityError(_DIMENSION_MISMATCH)
    if profile_contract.DEFAULT_CHUNKER_VERSION != chunking.CHUNKER_VERSION:
        raise WorkerIndexIdentityError(_CHUNKER_MISMATCH)
    return frozen_revision


@lru_cache(maxsize=1)
def initialize_worker_index_identity(
    model_directory: Path = DEFAULT_MODEL_DIRECTORY,
) -> WorkerIndexIdentity:
    """校验本地模型资产与冻结契约，返回进程内可缓存的只读索引身份。

    这是唯一入口：只有显式调用才做资产 IO 与 jieba 构造，成功结果按 ``model_directory``
    缓存（``maxsize=1``），失败不缓存。``.cache_clear()`` 只供测试使用。
    """

    try:
        token_counter = LocalTokenizerCounter(model_directory)
    except TokenCounterError:
        raise WorkerIndexIdentityError(_TOKENIZER_ASSET_FAILURE) from None

    frozen_revision = _verify_cross_source_constants()
    tokenizer_revision = _resolve_tokenizer_revision(frozen_revision)

    try:
        # 只构造一次：其 analyzer_id 直接进入契约，避免 default_index_profile() 再构造一份。
        keyword_analyzer = KeywordAnalyzer()
    except KeywordAnalyzerError:
        raise WorkerIndexIdentityError(_ANALYZER_FAILURE) from None

    try:
        profile = IndexProfileContract(
            embedding_model=profile_contract.DEFAULT_EMBEDDING_MODEL,
            model_revision=frozen_revision,
            dimension=profile_contract.DEFAULT_DIMENSION,
            normalize=profile_contract.DEFAULT_NORMALIZE,
            tokenizer_revision=tokenizer_revision,
            chunker_version=profile_contract.DEFAULT_CHUNKER_VERSION,
            keyword_analyzer_version=keyword_analyzer.analyzer_id,
        )
    except ProfileContractError:
        raise WorkerIndexIdentityError(_CONTRACT_FAILURE) from None

    from rag_backend.ingestion.docx_parsing import DOCX_PARSER_VERSION
    from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
    from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION

    return WorkerIndexIdentity(
        profile=profile,
        parser_version=MARKDOWN_PARSER_VERSION,
        pdf_parser_version=PDF_PARSER_VERSION,
        docx_parser_version=DOCX_PARSER_VERSION,
        token_counter=token_counter,
        keyword_analyzer=keyword_analyzer,
    )
