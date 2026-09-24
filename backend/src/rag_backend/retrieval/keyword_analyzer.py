"""版本化中文关键词分析器：查询与文档共用同一固定契约。

本模块只做纯本地文本处理：不联网、不读环境密钥、不接触数据库、Redis、Celery 或 HTTP，
也不修改 jieba 的全局默认 ``jieba.dt``。它把一个文本规范化成确定顺序的词项流，供后续
以参数绑定方式交给 PostgreSQL ``to_tsvector('simple', :param)``；本切片不连接数据库，
因此不声称任何真实 PG 检索已验收。

契约（与 [检索契约](../../../../docs/retrieval.md) 一致）：

- jieba 固定为 :data:`JIEBA_VERSION`，使用私有 :class:`jieba.Tokenizer` 与搜索模式
  ``cut_for_search``，不使用全局分词器，也不在运行期改词典。jieba 自带 ``dict.txt``
  已随 wheel 离线安装，运行期不下载任何词典。
- jieba 基础词典按原始字节大小与 SHA-256 钉死后再初始化；jieba 的 ``jieba.cache``
  默认写在系统 temp 且对默认词典不做 mtime 校验，本模块改为在独有的 0700 私有临时目录
  内构建缓存，构造结束（含异常）即删除该目录，绝不读写共享 ``temp/jieba.cache``，避免
  其他进程用伪造 marshal 改变分词结果而分析器标识不变。
- 领域词典是包内资源 ``domain-dictionary-v1.txt``，按原始字节 SHA-256 校验；当前 v1
  是 0 字节空词典（没有授权真实术语，不编造企业数据）。改动词典必须同时改版本、文件名、
  :data:`DOMAIN_DICTIONARY_SHA256` 与 profile，分析器标识随之变化。
- 规范化固定为 NFKC + Unicode ``casefold``：全角变半角、大小写折叠；英文标识、错误码、
  数字与下划线/连字符保留。NFKC 可能扩张字符数（例如 U+FDFA），因此原始长度与规范化后
  长度都要受 :data:`MAX_INPUT_CHARS` 约束。原文偏移不在此产生，引用仍用未归一化的
  ``chunk.source_locator``。
- 词项按 jieba 与标识符切分的确定顺序输出，保留重复词项（重复处理固定为不去重），
  纯标点、空白与 emoji 词项被排除。空输入产出空词流。

已知限制（不虚报可检索）：为使 ``错误码``、``gpt-4``、``v1.2.3`` 一类标识不被 jieba 拆散，
本模块用固定标识正则把 ASCII 标识整段保留为单个词项。真实 PostgreSQL 17 的 ``simple``
配置仍会按自己的规则再切分这段文本：``error_code`` 会丢掉下划线（结果与 ``error code``
相近），``gpt-4`` 会被拆成 ``gpt`` 与 ``-4``。因此本模块只保证输出形状，不声明标识一定
可被 FTS 命中。另：``analyze`` 的输出是给 ``to_tsvector`` 的**索引词流**，不是可执行的
``tsquery``；直接把它传给 ``to_tsquery``（尤其含 ``gpt-4`` 这类连字符）会语法错误，查询侧
必须自行用绑定参数构造合法 tsquery。带尾随运算符的 ``C++`` 这类写法只保留 ``c``。单次处理
超长文本的吞吐与延迟未验收，本切片只声明确定性输出契约与输入上限。
"""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Final

import jieba

# 冻结版本：换 jieba 版本必须改这里并重新核对搜索模式行为。
JIEBA_VERSION: Final = "0.42.1"

# 分析器 profile、领域词典版本与规范化一起进入分析器标识。
ANALYZER_PROFILE: Final = "search-v1"
DOMAIN_DICTIONARY_VERSION: Final = "v1"
DOMAIN_DICTIONARY_FILENAME: Final = "domain-dictionary-v1.txt"

# v1 是 0 字节空词典的 SHA-256；必须与仓库内资源逐字节一致。
DOMAIN_DICTIONARY_SHA256: Final = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# jieba 0.42.1 自带 dict.txt 的字节数与 SHA-256；防止基础词典静默漂移。
BASE_DICTIONARY_FILENAME: Final = "dict.txt"
BASE_DICTIONARY_SIZE: Final = 5071852
BASE_DICTIONARY_SHA256: Final = (
    "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
)

# 固定规范化描述；变更规则必须同时提升 profile。
NORMALIZATION: Final = "NFKC+casefold"

# 单块典型输入远小于全文；原始长度与规范化后长度都不得超过该字符数。
MAX_INPUT_CHARS: Final = 100_000

_PACKAGE: Final = "rag_backend.retrieval"
_CACHE_DIRECTORY_PREFIX: Final = "rag-backend-jieba-"

# ASCII 标识：以字母/数字开头结尾，允许下划线、点、连字符、加号、井号、& 作为内部分隔。
_IDENTIFIER: Final = re.compile(r"[0-9A-Za-z]+(?:[._\-+#&]+[0-9A-Za-z]+)*")

# 至少含一个字母或数字的 Unicode 词字符；用于丢弃纯标点、空白、emoji 与单独的下划线。
_RETAINED: Final = re.compile(r"[^\W_]", re.UNICODE)


class KeywordAnalyzerError(RuntimeError):
    """版本、词典资源或临时缓存不满足固定契约；构造失败即不可用。"""


class KeywordAnalyzerInputError(KeywordAnalyzerError):
    """输入类型或大小不符合契约；调用方应映射为 4xx 而非部署错误。"""


@dataclass(frozen=True, slots=True)
class KeywordAnalyzerProfile:
    """分析器身份：算法 profile、jieba 与两份词典的版本/摘要、规范化规则。"""

    profile: str
    jieba_version: str
    base_dictionary_sha256: str
    dictionary_version: str
    dictionary_sha256: str
    normalization: str

    @property
    def analyzer_id(self) -> str:
        """同时编码基础词典摘要、领域词典版本与摘要、规范化规则。"""

        return (
            f"jieba-{self.jieba_version}-{self.profile}"
            f":base-sha256={self.base_dictionary_sha256}"
            f":{self.dictionary_version}-sha256={self.dictionary_sha256}"
            f":norm={self.normalization}"
        )


def normalize_text(text: str) -> str:
    """固定规范化：NFKC 兼容分解 + Unicode casefold；不做分词、不改原文来源。"""

    return unicodedata.normalize("NFKC", text).casefold()


def load_base_dictionary_bytes() -> bytes:
    """读取 jieba 包内自带基础词典原始字节；缺失即静态失败。"""

    module_file = jieba.__file__
    if not isinstance(module_file, str):
        raise KeywordAnalyzerError("jieba 基础词典缺失")
    try:
        return Path(module_file).with_name(BASE_DICTIONARY_FILENAME).read_bytes()
    except OSError:
        raise KeywordAnalyzerError("jieba 基础词典缺失") from None


def verify_base_dictionary(data: bytes) -> str:
    """按字节数与 SHA-256 校验 jieba 基础词典属于固定版本，返回摘要。"""

    if len(data) != BASE_DICTIONARY_SIZE:
        raise KeywordAnalyzerError("jieba 基础词典大小与固定版本不符")
    digest = hashlib.sha256(data).hexdigest()
    if digest != BASE_DICTIONARY_SHA256:
        raise KeywordAnalyzerError("jieba 基础词典 SHA-256 与固定版本不符")
    return digest


def load_domain_dictionary_bytes() -> bytes:
    """从包内资源读取领域词典原始字节；缺失即静态失败，不回退到默认或其他词典。"""

    try:
        resource = resources.files(_PACKAGE).joinpath(DOMAIN_DICTIONARY_FILENAME)
        return resource.read_bytes()
    except (OSError, ModuleNotFoundError):
        raise KeywordAnalyzerError("领域词典包资源缺失") from None


def verify_domain_dictionary(data: bytes) -> str:
    """按原始字节 SHA-256 校验词典属于当前固定版本，返回摘要。"""

    digest = hashlib.sha256(data).hexdigest()
    if digest != DOMAIN_DICTIONARY_SHA256:
        raise KeywordAnalyzerError("领域词典 SHA-256 与固定版本不符")
    return digest


def _verified_jieba_version() -> str:
    version = str(jieba.__version__)
    if version != JIEBA_VERSION:
        raise KeywordAnalyzerError(f"jieba 版本不符：期望 {JIEBA_VERSION}")
    return version


def _build_private_tokenizer(data: bytes) -> Any:
    """在独有的受限临时目录内初始化分词器，退出即清理，绝不碰共享 ``jieba.cache``。"""

    tokenizer: Any = jieba.Tokenizer()
    try:
        cache_directory = tempfile.TemporaryDirectory(prefix=_CACHE_DIRECTORY_PREFIX)
    except OSError:
        raise KeywordAnalyzerError("关键词分析器临时缓存目录不可用") from None
    try:
        with cache_directory as cache_dir:
            # 必须在 load_userdict 触发初始化之前设置，否则会读写共享 temp/jieba.cache。
            tokenizer.tmp_dir = cache_dir
            try:
                tokenizer.load_userdict(io.BytesIO(data))
            except ValueError:
                raise KeywordAnalyzerError("领域词典格式非法") from None
            except OSError:
                raise KeywordAnalyzerError("领域词典加载失败") from None
    except KeywordAnalyzerError:
        raise
    except OSError:
        raise KeywordAnalyzerError("关键词分析器临时缓存目录不可用") from None
    return tokenizer


class KeywordAnalyzer:
    """单个分析器实例：私有 jieba 分词器 + 已校验领域词典，构造时完成全部校验。

    构造顺序固定为先校验 jieba 版本与基础词典字节、再校验领域词典字节、最后在独有临时
    目录内加载分词器：任何一步失败都以 :class:`KeywordAnalyzerError` 静态失败，且不触碰
    jieba 全局分词器或基础库 logger 状态。
    """

    def __init__(self) -> None:
        version = _verified_jieba_version()
        base_digest = verify_base_dictionary(load_base_dictionary_bytes())
        data = load_domain_dictionary_bytes()
        digest = verify_domain_dictionary(data)
        self._tokenizer: Any = _build_private_tokenizer(data)
        self._profile = KeywordAnalyzerProfile(
            profile=ANALYZER_PROFILE,
            jieba_version=version,
            base_dictionary_sha256=base_digest,
            dictionary_version=DOMAIN_DICTIONARY_VERSION,
            dictionary_sha256=digest,
            normalization=NORMALIZATION,
        )

    @property
    def profile(self) -> KeywordAnalyzerProfile:
        return self._profile

    @property
    def analyzer_id(self) -> str:
        return self._profile.analyzer_id

    def tokenize(self, text: str) -> tuple[str, ...]:
        """返回规范化后的确定顺序词项；重复词项保留，纯标点/空白/emoji 丢弃。"""

        if not isinstance(text, str):
            raise KeywordAnalyzerInputError("输入必须是字符串")
        if len(text) > MAX_INPUT_CHARS:
            raise KeywordAnalyzerInputError(f"输入超过 {MAX_INPUT_CHARS} 字符上限")
        normalized = normalize_text(text)
        if len(normalized) > MAX_INPUT_CHARS:
            raise KeywordAnalyzerInputError(f"规范化后超过 {MAX_INPUT_CHARS} 字符上限")
        terms: list[str] = []
        cursor = 0
        for match in _IDENTIFIER.finditer(normalized):
            start, end = match.span()
            if start > cursor:
                terms.extend(self._jieba_terms(normalized[cursor:start]))
            terms.append(match.group())
            cursor = end
        if cursor < len(normalized):
            terms.extend(self._jieba_terms(normalized[cursor:]))
        return tuple(terms)

    def analyze(self, text: str) -> str:
        """返回空白连接的词流，供 ``to_tsvector('simple', :param)`` 参数绑定使用。"""

        return " ".join(self.tokenize(text))

    def _jieba_terms(self, segment: str) -> list[str]:
        terms: list[str] = []
        for raw in self._tokenizer.cut_for_search(segment):
            token = raw.strip()
            if token and _RETAINED.search(token) is not None:
                terms.append(token)
        return terms


@lru_cache(maxsize=1)
def get_keyword_analyzer() -> KeywordAnalyzer:
    """返回进程内唯一分析器；首次调用即校验版本与词典，之后复用同一实例。"""

    return KeywordAnalyzer()
