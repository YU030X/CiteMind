"""worker 索引身份工厂单测：golden 契约、真实资产拒绝路径、缓存与依赖图约束。

本文件不联网、不下载模型、不连数据库/Celery。合成 tokenizer fixture 复用
``test_token_counting.py`` 的最小 BERT 结构，并同时 patch ``token_counting`` 与
``profile_contract`` 的冻结摘要表；真实 golden 只在宿主已具 ``/models`` 资产时运行，否则
显式 skip（不把 skip 计为 pass）。真实 KeywordAnalyzer 只被 golden 与 jieba 版本失配用例
构造，其余用例用轻量假分析器，避免反复初始化 jieba。
"""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import jieba
import pytest
from rag_backend.ingestion import (
    chunking,
    embedding_client,
    token_counting,
    worker_index_identity,
)
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.token_counting import (
    DEFAULT_MODEL_DIRECTORY,
    TokenCounterError,
)
from rag_backend.ingestion.worker_index_identity import (
    WorkerIndexIdentity,
    WorkerIndexIdentityError,
    initialize_worker_index_identity,
)
from rag_backend.models import profile_contract
from rag_backend.models.profile_contract import IndexProfileContract
from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzer, KeywordAnalyzerError
from tokenizers import Tokenizer, models, pre_tokenizers, processors

REPO_ROOT = Path(__file__).parents[2]
WORKER_SOURCE = REPO_ROOT / "backend" / "src" / "rag_backend" / "worker.py"

FIXTURE_FILES = ("tokenizer.json", "vocab.txt", "tokenizer_config.json", "special_tokens_map.json")

# 与 test_profile_contract.py 相同的一次性 golden；这里只用于核对工厂产出的契约。
GOLDEN_TOKENIZER_REVISION = (
    "bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620:"
    "tokenizer-artifacts-v1-sha256="
    "ca6e9808373afae7a8b131f50361c9b125ba5914eef0161b148b3ab6a105f9a8"
)
GOLDEN_KEYWORD_ANALYZER_VERSION = (
    "jieba-0.42.1-search-v1:base-sha256="
    "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8:"
    "v1-sha256="
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855:"
    "norm=NFKC+casefold"
)
GOLDEN_CONFIG_HASH = "4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa"


@pytest.fixture(autouse=True)
def _clear_identity_cache() -> Iterator[None]:
    """每个用例前后清空成功缓存；生产不提供 reset 接口。"""

    initialize_worker_index_identity.cache_clear()
    yield
    initialize_worker_index_identity.cache_clear()


class _FakeCounter:
    """只记录被构造的目录；用于跳过真实 tokenizer 资产校验。"""

    def __init__(self, model_directory: Path) -> None:
        self.model_directory = model_directory

    def count_tokens(self, text: str) -> int:
        return len(text)


class _FakeAnalyzer:
    """最小分析器替身：只需提供非空 analyzer_id 供契约构造。"""

    def __init__(self) -> None:
        self.analyzer_id = "jieba-0.42.1-search-v1:fake"


class _FailingCounter:
    """模拟真实计数器在含机密路径失败；用于断言错误消息静态脱敏。"""

    def __init__(self, model_directory: Path) -> None:
        raise TokenCounterError(f"tokenizer 模型目录不可读：{model_directory}")


def _raise_keyword_analyzer_error() -> KeywordAnalyzer:
    raise KeywordAnalyzerError("/secret/jieba-dictionary")


def _bogus_tokenizer_revision(*args: object, **kwargs: object) -> str:
    return "bogus"


def _forbidden_default_index_profile() -> IndexProfileContract:
    raise AssertionError("工厂不得调用 default_index_profile()")


def _fixture_tokenizer() -> Tokenizer:
    """与 test_token_counting 一致的最小 BERT fixture：显式添加 [CLS]/[SEP]。"""

    tokenizer = Tokenizer(
        models.WordLevel(
            vocab={"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "hello": 3, "world": 4},
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    return tokenizer


def _write_fixture_model_dir(base: Path) -> Path:
    model_dir = base / "bge-small-zh-v1.5"
    model_dir.mkdir(parents=True, exist_ok=True)
    _fixture_tokenizer().save(str(model_dir / "tokenizer.json"))
    (model_dir / "vocab.txt").write_text("[UNK]\n[CLS]\n[SEP]\nhello\nworld\n", encoding="utf-8")
    (model_dir / "tokenizer_config.json").write_text(
        '{"tokenizer_class": "BertTokenizer"}', encoding="utf-8"
    )
    (model_dir / "special_tokens_map.json").write_text(
        '{"unk_token": "[UNK]", "cls_token": "[CLS]", "sep_token": "[SEP]"}',
        encoding="utf-8",
    )
    return model_dir


def _artifact_table(model_dir: Path) -> dict[str, tuple[int, str]]:
    return {
        name: (
            (model_dir / name).stat().st_size,
            hashlib.sha256((model_dir / name).read_bytes()).hexdigest(),
        )
        for name in FIXTURE_FILES
    }


def _patch_synthetic_assets(
    monkeypatch: pytest.MonkeyPatch, model_dir: Path
) -> dict[str, tuple[int, str]]:
    """把 worker 与契约两侧的冻结摘要表同时指向合成 fixture，保持跨源一致性。"""

    table = _artifact_table(model_dir)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", table)
    monkeypatch.setattr(profile_contract, "TOKENIZER_ARTIFACTS", table)
    return table


# ---------------------------------------------------------------------------
# golden：七字段、tokenizer_revision、config_hash 与 parser_version
# ---------------------------------------------------------------------------


def test_identity_matches_frozen_golden_seven_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)

    identity = initialize_worker_index_identity(tmp_path / "golden-model")
    profile = identity.profile

    assert isinstance(identity, WorkerIndexIdentity)
    assert profile.embedding_model == profile_contract.DEFAULT_EMBEDDING_MODEL
    assert profile.embedding_model == "BAAI/bge-small-zh-v1.5"
    assert profile.model_revision == profile_contract.DEFAULT_MODEL_REVISION
    assert profile.dimension == profile_contract.DEFAULT_DIMENSION == 512
    assert profile.normalize is profile_contract.DEFAULT_NORMALIZE is True
    assert profile.tokenizer_revision == GOLDEN_TOKENIZER_REVISION
    assert profile.chunker_version == profile_contract.DEFAULT_CHUNKER_VERSION
    assert profile.chunker_version == "heading-pack-v1"
    assert profile.keyword_analyzer_version == GOLDEN_KEYWORD_ANALYZER_VERSION
    assert profile.config_hash() == GOLDEN_CONFIG_HASH
    assert identity.parser_version == MARKDOWN_PARSER_VERSION == "markdown-it-py-4.2.0-v1"
    # 真实 KeywordAnalyzer 构造一次并直接取自报 analyzer_id。
    assert isinstance(identity.keyword_analyzer, KeywordAnalyzer)
    assert identity.keyword_analyzer.analyzer_id == GOLDEN_KEYWORD_ANALYZER_VERSION
    assert isinstance(identity.token_counter, _FakeCounter)


def test_factory_does_not_call_default_index_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr(
        profile_contract, "default_index_profile", _forbidden_default_index_profile
    )

    identity = initialize_worker_index_identity(model_dir)

    assert identity.profile.keyword_analyzer_version == "jieba-0.42.1-search-v1:fake"


# ---------------------------------------------------------------------------
# 缓存：仅成功、按 Path 参数、依赖只构造一次
# ---------------------------------------------------------------------------


def test_factory_caches_single_identity_and_constructs_each_dependency_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    counters: list[_FakeCounter] = []
    analyzers: list[_FakeAnalyzer] = []

    def fake_counter(model_directory: Path) -> _FakeCounter:
        counter = _FakeCounter(model_directory)
        counters.append(counter)
        return counter

    def fake_analyzer() -> _FakeAnalyzer:
        analyzer = _FakeAnalyzer()
        analyzers.append(analyzer)
        return analyzer

    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", fake_counter)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", fake_analyzer)

    first = initialize_worker_index_identity(model_dir)
    second = initialize_worker_index_identity(model_dir)

    assert first is second
    assert first.token_counter is second.token_counter
    assert first.keyword_analyzer is second.keyword_analyzer
    assert len(counters) == 1
    assert len(analyzers) == 1


def test_factory_cache_is_keyed_by_model_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dir_a = _write_fixture_model_dir(tmp_path / "a")
    dir_b = _write_fixture_model_dir(tmp_path / "b")
    _patch_synthetic_assets(monkeypatch, dir_a)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)

    identity_a = initialize_worker_index_identity(dir_a)
    identity_b = initialize_worker_index_identity(dir_b)

    assert identity_a is not identity_b
    assert identity_a.token_counter is not identity_b.token_counter


def test_first_failure_is_not_cached_and_later_success_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)
    original = (model_dir / "vocab.txt").read_bytes()
    (model_dir / "vocab.txt").write_bytes(b"tampered")

    with pytest.raises(WorkerIndexIdentityError):
        initialize_worker_index_identity(model_dir)

    # 失败不入缓存：修复同一路径后即可成功，无需清理缓存。
    (model_dir / "vocab.txt").write_bytes(original)
    identity = initialize_worker_index_identity(model_dir)

    assert identity.profile.config_hash() != ""
    assert identity.token_counter.count_tokens("hello world") == 4


# ---------------------------------------------------------------------------
# 拒绝路径：任一资产、文件集合、关键词分析器、跨源常量
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_factory_rejects_any_tampered_asset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)
    path = model_dir / name
    data = path.read_bytes()
    path.write_bytes(bytes(byte ^ 0xFF for byte in data))

    with pytest.raises(WorkerIndexIdentityError, match="tokenizer"):
        initialize_worker_index_identity(model_dir)


def test_factory_rejects_missing_asset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)
    (model_dir / "vocab.txt").unlink()

    with pytest.raises(WorkerIndexIdentityError, match="tokenizer"):
        initialize_worker_index_identity(model_dir)


def test_factory_rejects_extra_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)
    (model_dir / "model.safetensors").write_bytes(b"not-a-weight")

    with pytest.raises(WorkerIndexIdentityError, match="tokenizer"):
        initialize_worker_index_identity(model_dir)


def test_factory_rejects_tampered_artifact_before_keyword_analyzer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 关键词分析器若被构造会显式失败：资产校验必须先失败，证明顺序固定。
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _raise_keyword_analyzer_error)
    (model_dir / "vocab.txt").write_bytes(b"tampered")

    with pytest.raises(WorkerIndexIdentityError, match="tokenizer"):
        initialize_worker_index_identity(model_dir)


def test_factory_rejects_jieba_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(jieba, "__version__", "0.0.0")

    with pytest.raises(WorkerIndexIdentityError, match="关键词"):
        initialize_worker_index_identity(model_dir)


def test_factory_rejects_cross_source_artifact_table_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)
    mismatched = dict(profile_contract.TOKENIZER_ARTIFACTS)
    name = next(iter(mismatched))
    size, _digest = mismatched[name]
    mismatched[name] = (size, "0" * 64)
    monkeypatch.setattr(profile_contract, "TOKENIZER_ARTIFACTS", mismatched)

    with pytest.raises(WorkerIndexIdentityError, match="跨源"):
        initialize_worker_index_identity(tmp_path)


def test_factory_rejects_embedding_revision_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)
    monkeypatch.setattr(embedding_client, "EXPECTED_MODEL_REVISION", "0" * 40)

    with pytest.raises(WorkerIndexIdentityError, match="revision"):
        initialize_worker_index_identity(tmp_path)


def test_factory_rejects_embedding_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)
    monkeypatch.setattr(embedding_client, "EMBEDDING_DIMENSION", 768)

    with pytest.raises(WorkerIndexIdentityError, match="维度"):
        initialize_worker_index_identity(tmp_path)


def test_factory_rejects_chunker_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)
    monkeypatch.setattr(chunking, "CHUNKER_VERSION", "heading-pack-v2")

    with pytest.raises(WorkerIndexIdentityError, match="切分器"):
        initialize_worker_index_identity(tmp_path)


def test_factory_rejects_tokenizer_revision_without_frozen_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)
    monkeypatch.setattr(profile_contract, "tokenizer_revision", _bogus_tokenizer_revision)

    with pytest.raises(WorkerIndexIdentityError, match="tokenizer_revision"):
        initialize_worker_index_identity(tmp_path)


def test_factory_rejects_invalid_contract_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 冻结常量被改成 ``normalize=False`` 时，契约 ``__post_init__`` 必须静态失败。
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FakeCounter)
    monkeypatch.setattr(profile_contract, "DEFAULT_NORMALIZE", False)

    with pytest.raises(WorkerIndexIdentityError, match="契约"):
        initialize_worker_index_identity(tmp_path)


# ---------------------------------------------------------------------------
# 静态脱敏：不泄漏目录、DSN 或原始异常链
# ---------------------------------------------------------------------------


def test_tokenizer_failure_is_static_and_hides_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = tmp_path / "secret-model-dir"
    monkeypatch.setattr(worker_index_identity, "LocalTokenizerCounter", _FailingCounter)

    with pytest.raises(WorkerIndexIdentityError, match="tokenizer") as info:
        initialize_worker_index_identity(model_dir)

    message = str(info.value)
    assert str(model_dir) not in message
    assert "secret-model-dir" not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_keyword_failure_is_static_and_hides_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _raise_keyword_analyzer_error)

    with pytest.raises(WorkerIndexIdentityError, match="关键词") as info:
        initialize_worker_index_identity(model_dir)

    message = str(info.value)
    assert "/secret" not in message
    assert str(model_dir) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# 只读性与依赖图约束
# ---------------------------------------------------------------------------


def test_identity_and_profile_are_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    _patch_synthetic_assets(monkeypatch, model_dir)
    monkeypatch.setattr(worker_index_identity, "KeywordAnalyzer", _FakeAnalyzer)

    identity = initialize_worker_index_identity(model_dir)

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(identity, "parser_version", "markdown-v1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(identity.profile, "chunker_version", "heading-pack-v2")


def test_worker_entrypoint_has_no_index_identity_hook() -> None:
    source = WORKER_SOURCE.read_text(encoding="utf-8")

    assert "worker_index_identity" not in source
    assert "initialize_worker_index_identity" not in source
    assert "token_counting" not in source


def test_importing_ingestion_package_does_not_load_tokenizers() -> None:
    code = (
        "import sys; import rag_backend.ingestion; "
        "assert 'tokenizers' not in sys.modules, 'tokenizers leaked into API import'; "
        "assert 'rag_backend.ingestion.worker_index_identity' not in sys.modules"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_importing_worker_module_loads_tokenizers_without_db_or_celery() -> None:
    code = (
        "import sys; import rag_backend.ingestion.worker_index_identity; "
        "assert 'tokenizers' in sys.modules, 'worker module must load tokenizers'; "
        "assert 'celery' not in sys.modules, 'celery leaked'; "
        "assert 'rag_backend.worker' not in sys.modules, 'worker leaked'; "
        "assert 'rag_backend.database' not in sys.modules, 'database leaked'"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 真实资产 golden：宿主没有烘入产物时显式 skip，不把 skip 计为 pass
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not DEFAULT_MODEL_DIRECTORY.is_dir(),
    reason="宿主没有烘入的 BGE tokenizer 产物；真实对照须由独立 tester 在 worker 镜像内复核",
)
def test_real_model_directory_identity_matches_golden() -> None:
    identity = initialize_worker_index_identity(DEFAULT_MODEL_DIRECTORY)

    assert identity.profile.tokenizer_revision == GOLDEN_TOKENIZER_REVISION
    assert identity.profile.keyword_analyzer_version == GOLDEN_KEYWORD_ANALYZER_VERSION
    assert identity.profile.config_hash() == GOLDEN_CONFIG_HASH
    assert identity.parser_version == MARKDOWN_PARSER_VERSION
    assert identity.token_counter.count_tokens("hello world") >= 1
