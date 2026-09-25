"""index profile 契约单测：七字段、规范 JSON、``config_hash`` 与跨源防漂移。

本文件是纯本地断言：不连数据库、不写 seed、不激活 KB、不联网、不加载真实模型。它冻结
一次算出的 golden 规范字节 / tokenizer_revision / config_hash，并把本模块常量与
:mod:`rag_backend.ingestion.embedding_client`、:mod:`rag_backend.ingestion.chunking`、
:mod:`rag_backend.ingestion.token_counting` 以及 inference 源码中的冻结身份交叉核对；缺少
任一上游常量即显式失败，不跳过。关键词分析器身份只按需构造一次（真实 jieba 已随 dev 组
安装）。本文件不验收数据库唯一约束、seed 或并发。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from rag_backend.ingestion import chunking, embedding_client, token_counting
from rag_backend.models import profile_contract
from rag_backend.models.profile_contract import (
    DEFAULT_CHUNKER_VERSION,
    DEFAULT_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MODEL_REVISION,
    DEFAULT_NORMALIZE,
    TOKENIZER_ARTIFACTS,
    IndexProfileContract,
    ProfileContractError,
    current_keyword_analyzer_version,
    default_index_profile,
    tokenizer_artifacts_digest,
    tokenizer_revision,
)
from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzer

REPO_ROOT = Path(__file__).parents[2]
INFERENCE_CONFIG = REPO_ROOT / "inference" / "src" / "citemind_inference" / "config.py"
INFERENCE_IDENTITY = REPO_ROOT / "inference" / "src" / "citemind_inference" / "model_identity.py"

# 一次算出后冻结的 golden：任何字段、摘要或规范 JSON 变化都必须显式更新这些常量。
GOLDEN_ARTIFACTS_CANONICAL = (
    '{"special_tokens_map.json":{"sha256":'
    '"b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3","size":125},'
    '"tokenizer.json":{"sha256":'
    '"48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26","size":439125},'
    '"tokenizer_config.json":{"sha256":'
    '"e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a","size":367},'
    '"vocab.txt":{"sha256":'
    '"45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c","size":109540}}'
)
GOLDEN_ARTIFACTS_DIGEST = "ca6e9808373afae7a8b131f50361c9b125ba5914eef0161b148b3ab6a105f9a8"
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
GOLDEN_CONFIG_CANONICAL = (
    '{"chunker_version":"heading-pack-v1","dimension":512,'
    '"embedding_model":"BAAI/bge-small-zh-v1.5",'
    '"keyword_analyzer_version":"jieba-0.42.1-search-v1:'
    "base-sha256=7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8:"
    "v1-sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855:"
    'norm=NFKC+casefold",'
    '"model_revision":"7999e1d3359715c523056ef9478215996d62a620",'
    '"normalize":true,'
    '"schema_version":"index-profile-v1",'
    '"tokenizer_revision":"bge-small-zh-v1.5@'
    "7999e1d3359715c523056ef9478215996d62a620:"
    "tokenizer-artifacts-v1-sha256="
    'ca6e9808373afae7a8b131f50361c9b125ba5914eef0161b148b3ab6a105f9a8"}'
)
GOLDEN_CONFIG_HASH = "4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa"

_GOLDEN_REVISION = "7999e1d3359715c523056ef9478215996d62a620"


def _module_constant(path: Path, name: str) -> Any:
    """从 Python 源码取出一个顶层字面量赋值；用于跨包防漂移核对常量。"""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return cast("Any", ast.literal_eval(node.value))
        elif isinstance(node, ast.Assign):
            targets = [target for target in node.targets if isinstance(target, ast.Name)]
            if any(target.id == name for target in targets):
                return cast("Any", ast.literal_eval(node.value))
    raise AssertionError(f"{path} 缺少顶层字面量 {name}")


def _contract_with(**changes: object) -> Any:
    """用任意字段值重建契约，绕过 ``replace`` 的静态字段类型；仅用于校验路径测试。"""

    return cast("Any", dataclasses.replace)(default_index_profile(), **changes)


# ---------------------------------------------------------------------------
# golden 回归：默认七字段、规范字节与 config_hash
# ---------------------------------------------------------------------------


def test_default_profile_fields_match_frozen_constants() -> None:
    profile = default_index_profile()

    assert profile.embedding_model == DEFAULT_EMBEDDING_MODEL == "BAAI/bge-small-zh-v1.5"
    assert profile.model_revision == DEFAULT_MODEL_REVISION == _GOLDEN_REVISION
    assert profile.dimension == DEFAULT_DIMENSION == 512
    assert profile.normalize is DEFAULT_NORMALIZE is True
    assert profile.tokenizer_revision == GOLDEN_TOKENIZER_REVISION
    assert profile.chunker_version == DEFAULT_CHUNKER_VERSION == "heading-pack-v1"
    assert profile.keyword_analyzer_version == GOLDEN_KEYWORD_ANALYZER_VERSION


def test_tokenizer_artifacts_cover_exactly_the_four_files() -> None:
    assert tuple(TOKENIZER_ARTIFACTS) == (
        "tokenizer.json",
        "vocab.txt",
        "tokenizer_config.json",
        "special_tokens_map.json",
    )
    assert "config.json" not in TOKENIZER_ARTIFACTS
    assert "model.safetensors" not in TOKENIZER_ARTIFACTS


def test_golden_tokenizer_artifacts_canonical_bytes_and_digest() -> None:
    payload = {
        name: {"size": size, "sha256": sha256}
        for name, (size, sha256) in TOKENIZER_ARTIFACTS.items()
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    assert canonical.decode("utf-8") == GOLDEN_ARTIFACTS_CANONICAL
    assert hashlib.sha256(canonical).hexdigest() == GOLDEN_ARTIFACTS_DIGEST
    assert tokenizer_artifacts_digest() == GOLDEN_ARTIFACTS_DIGEST


def test_golden_tokenizer_revision() -> None:
    assert tokenizer_revision() == GOLDEN_TOKENIZER_REVISION
    assert tokenizer_revision().startswith(f"bge-small-zh-v1.5@{_GOLDEN_REVISION}:")


def test_golden_config_canonical_bytes_and_hash() -> None:
    profile = default_index_profile()
    digest = profile.config_hash()

    assert profile.canonical_bytes().decode("utf-8") == GOLDEN_CONFIG_CANONICAL
    assert digest == GOLDEN_CONFIG_HASH
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(char in "0123456789abcdef" for char in digest)


def test_canonical_bytes_are_plain_utf8_without_bom_or_newline() -> None:
    raw = default_index_profile().canonical_bytes()

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in raw
    assert b"\r" not in raw


def test_default_profile_is_reproducible() -> None:
    assert default_index_profile() == default_index_profile()
    assert default_index_profile().config_hash() == default_index_profile().config_hash()


# ---------------------------------------------------------------------------
# 敏感性：字段、schema_version、摘要变化都必须改变 hash
# ---------------------------------------------------------------------------


def test_config_hash_is_insensitive_to_field_construction_order() -> None:
    base = default_index_profile()
    reordered = IndexProfileContract(
        keyword_analyzer_version=base.keyword_analyzer_version,
        chunker_version=base.chunker_version,
        tokenizer_revision=base.tokenizer_revision,
        normalize=base.normalize,
        dimension=base.dimension,
        model_revision=base.model_revision,
        embedding_model=base.embedding_model,
    )

    assert reordered == base
    assert reordered.canonical_bytes() == base.canonical_bytes()
    assert reordered.config_hash() == base.config_hash()


def test_config_hash_changes_for_each_mutable_field_value() -> None:
    base = default_index_profile()
    mutations: dict[str, object] = {
        "embedding_model": "BAAI/other-model",
        "model_revision": "0" * 40,
        "tokenizer_revision": "bge-small-zh-v1.5@other:tokenizer-artifacts-v1-sha256=" + "0" * 64,
        "chunker_version": "heading-pack-v2",
        "keyword_analyzer_version": "jieba-0.42.1-search-v2",
    }

    for name, value in mutations.items():
        mutated = _contract_with(**{name: value})
        assert mutated.config_hash() != base.config_hash(), name


def test_config_hash_changes_when_schema_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_hash = default_index_profile().config_hash()

    monkeypatch.setattr(profile_contract, "PROFILE_SCHEMA_VERSION", "index-profile-v2")

    assert default_index_profile().config_hash() != base_hash


def test_tokenizer_digest_changes_when_artifact_digest_changes() -> None:
    modified = dict(TOKENIZER_ARTIFACTS)
    name, (size, _digest) = next(iter(modified.items()))
    modified[name] = (size, "0" * 64)

    assert tokenizer_artifacts_digest(modified) != GOLDEN_ARTIFACTS_DIGEST
    assert tokenizer_revision(artifacts=modified) != GOLDEN_TOKENIZER_REVISION


# ---------------------------------------------------------------------------
# 校验：非法维度 / 类型 / 空字符串静态失败；实例不可变
# ---------------------------------------------------------------------------


def test_invalid_dimension_is_rejected() -> None:
    for dimension in (768, 0, -512, True, "512"):
        with pytest.raises(ProfileContractError):
            _contract_with(dimension=dimension)


def test_invalid_normalize_is_rejected() -> None:
    for normalize in (False, 1, 0, "true"):
        with pytest.raises(ProfileContractError):
            _contract_with(normalize=normalize)


def test_blank_or_non_string_fields_are_rejected() -> None:
    for name in (
        "embedding_model",
        "model_revision",
        "tokenizer_revision",
        "chunker_version",
        "keyword_analyzer_version",
    ):
        with pytest.raises(ProfileContractError):
            _contract_with(**{name: ""})
        with pytest.raises(ProfileContractError):
            _contract_with(**{name: 123})


def test_profile_instance_is_immutable() -> None:
    profile = default_index_profile()

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(profile, "chunker_version", "heading-pack-v2")


def test_positional_argument_construction_is_rejected() -> None:
    profile = default_index_profile()

    with pytest.raises(TypeError):
        cast("Any", IndexProfileContract)(
            profile.embedding_model,
            profile.model_revision,
            profile.dimension,
            profile.normalize,
            profile.tokenizer_revision,
            profile.chunker_version,
            profile.keyword_analyzer_version,
        )


# ---------------------------------------------------------------------------
# 关键词分析器身份：构造一次、与 golden 和分析器自报一致
# ---------------------------------------------------------------------------


def test_keyword_analyzer_version_matches_analyzer_identity() -> None:
    assert current_keyword_analyzer_version() == GOLDEN_KEYWORD_ANALYZER_VERSION
    assert current_keyword_analyzer_version() == KeywordAnalyzer().analyzer_id
    assert default_index_profile().keyword_analyzer_version == current_keyword_analyzer_version()


# ---------------------------------------------------------------------------
# 防漂移：backend 常量、worker 摘要与 inference 冻结身份一致（缺失即失败）
# ---------------------------------------------------------------------------


def test_constants_match_backend_embedding_and_chunking_sources() -> None:
    assert DEFAULT_EMBEDDING_MODEL == "BAAI/bge-small-zh-v1.5"
    assert DEFAULT_MODEL_REVISION == embedding_client.EXPECTED_MODEL_REVISION
    assert DEFAULT_DIMENSION == embedding_client.EMBEDDING_DIMENSION == 512
    assert DEFAULT_CHUNKER_VERSION == chunking.CHUNKER_VERSION


def test_tokenizer_artifacts_match_worker_token_counting() -> None:
    assert dict(token_counting.TOKENIZER_ARTIFACTS) == dict(TOKENIZER_ARTIFACTS)


def test_constants_match_inference_frozen_identity() -> None:
    assert _module_constant(INFERENCE_CONFIG, "FROZEN_EMBEDDING_MODEL") == DEFAULT_EMBEDDING_MODEL
    assert _module_constant(INFERENCE_CONFIG, "FROZEN_EMBEDDING_REVISION") == DEFAULT_MODEL_REVISION
    assert _module_constant(INFERENCE_CONFIG, "EMBEDDING_DIMENSION") == DEFAULT_DIMENSION
    digests = _module_constant(INFERENCE_IDENTITY, "MODEL_ARTIFACT_DIGESTS")
    for name, entry in TOKENIZER_ARTIFACTS.items():
        assert digests[name] == entry, name


# ---------------------------------------------------------------------------
# api 镜像约束：导入本模块不得引入 tokenizers / torch / transformers / jieba
# ---------------------------------------------------------------------------


def test_module_import_does_not_pull_heavy_runtime_dependencies() -> None:
    code = (
        "import sys; import rag_backend.models.profile_contract; "
        "forbidden = {'tokenizers', 'torch', 'transformers', 'jieba'}; "
        "loaded = forbidden & set(sys.modules); "
        "assert not loaded, sorted(loaded)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
