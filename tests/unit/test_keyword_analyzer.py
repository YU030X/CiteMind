"""中文关键词分析器单测：固定版本、包内词典、私有缓存隔离、规范化与输入边界。

本文件只做纯本地断言：不连接 PostgreSQL、不联网、不加载真实模型。重点覆盖查询与文档共用
同一规范化、全角折叠、英文代码标识保留、emoji/标点排除、基础词典与领域词典篡改 failfast、
共享 ``jieba.cache`` 污染不生效、私有缓存目录无遗留、空词流、确定性、无全局 jieba 状态
污染，以及原始/规范化后超限的静态失败。PostgreSQL ``to_tsvector('simple')`` 对标识的再
切分仍未在真实数据库上由本文件验收，本文件不据此声称可检索。
"""

from __future__ import annotations

import hashlib
import marshal
import socket
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import jieba
import pytest
from rag_backend.retrieval import keyword_analyzer as ka
from rag_backend.retrieval.keyword_analyzer import (
    ANALYZER_PROFILE,
    BASE_DICTIONARY_SHA256,
    BASE_DICTIONARY_SIZE,
    DOMAIN_DICTIONARY_FILENAME,
    DOMAIN_DICTIONARY_SHA256,
    DOMAIN_DICTIONARY_VERSION,
    JIEBA_VERSION,
    MAX_INPUT_CHARS,
    NORMALIZATION,
    KeywordAnalyzer,
    KeywordAnalyzerError,
    KeywordAnalyzerInputError,
    get_keyword_analyzer,
    load_base_dictionary_bytes,
    load_domain_dictionary_bytes,
    normalize_text,
    verify_base_dictionary,
    verify_domain_dictionary,
)

REPO_ROOT = Path(__file__).parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CACHE_PREFIX = "rag-backend-jieba-"


def test_analyzer_id_encodes_version_profile_and_dictionary_hash() -> None:
    analyzer = KeywordAnalyzer()

    assert analyzer.analyzer_id == (
        f"jieba-{JIEBA_VERSION}-{ANALYZER_PROFILE}"
        f":base-sha256={BASE_DICTIONARY_SHA256}"
        f":{DOMAIN_DICTIONARY_VERSION}-sha256={DOMAIN_DICTIONARY_SHA256}"
        f":norm={NORMALIZATION}"
    )
    assert analyzer.profile.base_dictionary_sha256 == BASE_DICTIONARY_SHA256
    assert analyzer.profile.jieba_version == JIEBA_VERSION
    assert analyzer.profile.dictionary_version == DOMAIN_DICTIONARY_VERSION == "v1"
    assert analyzer.profile.normalization == "NFKC+casefold"


def test_profile_changes_alter_analyzer_id() -> None:
    profile = KeywordAnalyzer().profile

    assert profile.analyzer_id != replace(profile, normalization="NFKC").analyzer_id
    assert profile.analyzer_id != replace(profile, dictionary_version="v2").analyzer_id
    assert profile.analyzer_id != replace(profile, base_dictionary_sha256="0" * 64).analyzer_id
    assert profile.analyzer_id != replace(profile, jieba_version="0.0.0").analyzer_id


def test_base_dictionary_is_pinned_to_size_and_hash() -> None:
    data = load_base_dictionary_bytes()

    assert len(data) == BASE_DICTIONARY_SIZE
    assert hashlib.sha256(data).hexdigest() == BASE_DICTIONARY_SHA256
    assert verify_base_dictionary(data) == BASE_DICTIONARY_SHA256


def test_base_dictionary_tamper_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(KeywordAnalyzerError, match="基础词典大小"):
        verify_base_dictionary(b"not a dictionary")
    with pytest.raises(KeywordAnalyzerError, match="基础词典 SHA-256"):
        verify_base_dictionary(b"x" * BASE_DICTIONARY_SIZE)

    monkeypatch.setattr(ka, "load_base_dictionary_bytes", lambda: b"not a dictionary")
    with pytest.raises(KeywordAnalyzerError, match="基础词典"):
        KeywordAnalyzer()


def test_package_resource_is_empty_and_matches_pinned_hash() -> None:
    data = load_domain_dictionary_bytes()

    assert data == b""
    assert hashlib.sha256(data).hexdigest() == DOMAIN_DICTIONARY_SHA256
    assert DOMAIN_DICTIONARY_VERSION in DOMAIN_DICTIONARY_FILENAME


def test_wheel_target_includes_package_resources_without_exclusions() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel["packages"] == ["backend/src/rag_backend"]
    assert "exclude" not in wheel


def test_dictionary_tamper_fails_fast() -> None:
    with pytest.raises(KeywordAnalyzerError, match="SHA-256"):
        verify_domain_dictionary("错误码 100000 nz\n".encode())
    with pytest.raises(KeywordAnalyzerError, match="SHA-256"):
        verify_domain_dictionary(b"\n")


def test_analyzer_rejects_tampered_package_dictionary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ka, "load_domain_dictionary_bytes", lambda: "错误码 100000 nz\n".encode()
    )

    with pytest.raises(KeywordAnalyzerError, match="SHA-256"):
        KeywordAnalyzer()


def test_jieba_version_is_pinned_and_mismatch_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert jieba.__version__ == JIEBA_VERSION == "0.42.1"
    monkeypatch.setattr(jieba, "__version__", "0.0.0")

    with pytest.raises(KeywordAnalyzerError, match="jieba 版本不符"):
        KeywordAnalyzer()


def test_poisoned_shared_jieba_cache_does_not_change_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共享 ``jieba.cache`` 被 marshal 伪造时必须被忽略；私有缓存目录隔离之。"""

    (tmp_path / "jieba.cache").write_bytes(marshal.dumps(({}, 1)))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    analyzer = KeywordAnalyzer()
    text = "清华大学计算机"
    terms = analyzer.tokenize(text)

    # 真实词典会展开 2-gram；空 FREQ 的污染缓存不会。
    assert "清华" in terms
    assert terms == KeywordAnalyzer().tokenize(text)


def test_owned_cache_directory_is_removed_after_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poisoned = tmp_path / "jieba.cache"
    poisoned.write_bytes(marshal.dumps(({}, 1)))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = poisoned.read_bytes()

    KeywordAnalyzer()

    assert poisoned.read_bytes() == before
    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(CACHE_PREFIX)]
    assert leftovers == []


def test_unusable_temp_cache_directory_fails_statically_without_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leak = "C:/private/secret/cache/location"

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(leak)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _fail)

    with pytest.raises(KeywordAnalyzerError) as excinfo:
        KeywordAnalyzer()

    assert leak not in str(excinfo.value)
    assert not isinstance(excinfo.value, KeywordAnalyzerInputError)


def test_jieba_ships_its_dictionary_offline() -> None:
    assert jieba.__file__ is not None
    bundled = Path(jieba.__file__).with_name("dict.txt")

    assert bundled.is_file()
    assert bundled.stat().st_size > 0


def test_query_and_document_share_identical_normalization() -> None:
    analyzer = KeywordAnalyzer()

    assert analyzer.analyze("数据库 ABC") == analyzer.analyze("数据库　ＡＢＣ")
    assert analyzer.tokenize("数据库 ABC") == analyzer.tokenize("数据库　ＡＢＣ")


def test_full_width_and_casefold_normalization() -> None:
    assert normalize_text("ＡＢＣ１２３") == "abc123"
    assert normalize_text("Straße") == "strasse"
    assert normalize_text("ERR_Code") == "err_code"

    analyzer = KeywordAnalyzer()
    assert analyzer.tokenize("ＡＢＣ１２３") == ("abc123",)
    assert analyzer.tokenize("STRASSE") == analyzer.tokenize("straße") == ("strasse",)


def test_english_identifiers_are_preserved_as_single_terms() -> None:
    analyzer = KeywordAnalyzer()

    assert analyzer.tokenize("error_code") == ("error_code",)
    assert analyzer.tokenize("gpt-4") == ("gpt-4",)
    assert analyzer.tokenize("v1.2.3") == ("v1.2.3",)
    assert analyzer.tokenize("http-404 err_4012") == ("http-404", "err_4012")


def test_pg_simple_reparse_of_identifiers_is_not_verified_here() -> None:
    """本模块只保证 Python 词流不拆散标识；PG ``simple`` 的再切分未在真实库核对。

    ``error_code`` 与 ``error code`` 在本模块中是不同的词流；但 PostgreSQL 默认解析器
    会丢掉下划线、也会拆开 ``gpt-4``。这里只断言本模块的输出形状，不虚报标识一定可被
    FTS 命中，也不把该词流当作可直接执行的 tsquery。
    """

    analyzer = KeywordAnalyzer()

    assert analyzer.tokenize("error_code") == ("error_code",)
    assert analyzer.tokenize("error code") == ("error", "code")


def test_emoji_and_punctuation_are_excluded() -> None:
    analyzer = KeywordAnalyzer()

    assert analyzer.tokenize("你好，世界！😀🎉") == ("你好", "世界")
    assert analyzer.tokenize("_") == ()
    assert analyzer.analyze("，。！？？") == ""
    assert analyzer.analyze("😀😀") == ""
    assert analyzer.analyze("") == ""
    assert analyzer.analyze("   \t\n") == ""


def test_duplicate_terms_are_preserved_in_input_order() -> None:
    analyzer = KeywordAnalyzer()

    assert analyzer.tokenize("测试 测试 测试") == ("测试", "测试", "测试")


def test_tokenization_is_deterministic_across_instances() -> None:
    analyzer = KeywordAnalyzer()
    text = "数据库错误码 error_code 😀 测试"

    assert analyzer.tokenize(text) == analyzer.tokenize(text)
    assert analyzer.analyze(text) == analyzer.analyze(text)
    assert KeywordAnalyzer().tokenize(text) == analyzer.tokenize(text)


def test_input_errors_are_a_distinct_subclass() -> None:
    assert issubclass(KeywordAnalyzerInputError, KeywordAnalyzerError)
    analyzer = KeywordAnalyzer()

    with pytest.raises(KeywordAnalyzerInputError, match="字符上限"):
        analyzer.tokenize("x" * (MAX_INPUT_CHARS + 1))
    with pytest.raises(KeywordAnalyzerInputError, match="字符串"):
        analyzer.tokenize(123)  # type: ignore[arg-type]

    with pytest.raises(KeywordAnalyzerError) as excinfo:
        verify_domain_dictionary(b"tampered")
    assert not isinstance(excinfo.value, KeywordAnalyzerInputError)


def test_nfkc_expansion_is_rechecked_after_normalization() -> None:
    expansion = normalize_text("\ufdfa")
    assert len(expansion) > 1

    repeats = MAX_INPUT_CHARS // len(expansion) + 1
    text = "\ufdfa" * repeats
    assert len(text) <= MAX_INPUT_CHARS
    assert len(normalize_text(text)) > MAX_INPUT_CHARS

    with pytest.raises(KeywordAnalyzerInputError, match="规范化后"):
        KeywordAnalyzer().tokenize(text)


def test_no_network_and_no_global_jieba_pollution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert jieba.dt.initialized is False

    def _forbid_socket(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("关键词分析器不应创建 socket")

    monkeypatch.setattr(socket, "socket", _forbid_socket)
    analyzer = KeywordAnalyzer()
    assert analyzer.tokenize("错误码 error_code") == (
        "错误",
        "误码",
        "错误码",
        "error_code",
    )

    assert jieba.dt.initialized is False
    assert analyzer._tokenizer is not jieba.dt  # noqa: SLF001 - 私有检查用于隔离全局状态


def test_analyzer_leaves_jieba_logger_untouched() -> None:
    level = jieba.default_logger.level
    handlers = list(jieba.default_logger.handlers)

    analyzer = KeywordAnalyzer()
    analyzer.tokenize("测试")

    assert jieba.default_logger.level == level
    assert list(jieba.default_logger.handlers) == handlers


def test_get_keyword_analyzer_is_process_singleton() -> None:
    assert get_keyword_analyzer() is get_keyword_analyzer()


def test_module_has_no_database_or_http_imports() -> None:
    assert ka.__file__ is not None
    source = Path(ka.__file__).read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "psycopg" not in source
    assert "httpx" not in source
