"""Worker 本地真实 token 计数能力的聚焦单测：字节校验、拒绝路径与特殊 token 计数。

这些测试用纯 ``tokenizers`` 构造最小 fixture，不加载真实 BGE 权重、不导入 torch、不联网。
真实 BGE 产物的 golden 对照只有在 ``/models/bge-small-zh-v1.5`` 已具备时才运行，否则显式
跳过；与 inference ``AutoTokenizer`` 的语义等价仍须由独立 tester 在隔离容器内复核。
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from rag_backend.ingestion import token_counting
from rag_backend.ingestion.token_counting import (
    DEFAULT_MODEL_DIRECTORY,
    TOKENIZER_ARTIFACTS,
    LocalTokenizerCounter,
    TokenCounterError,
    verify_tokenizer_directory,
)
from tokenizers import Encoding, Tokenizer, models, pre_tokenizers, processors

REPO_ROOT = Path(__file__).parents[2]
INFERENCE_IDENTITY = REPO_ROOT / "inference" / "src" / "citemind_inference" / "model_identity.py"
INFERENCE_SCRIPT = REPO_ROOT / "inference" / "scripts" / "prepare_model.py"
GOLDEN_REFERENCE = REPO_ROOT / "inference" / "tests" / "golden" / "golden-reference.json"
SHARED_DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yml"
QUEUE_COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "queue.yml"

FIXTURE_FILES = ("tokenizer.json", "vocab.txt", "tokenizer_config.json", "special_tokens_map.json")


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


def _fixture_tokenizer() -> Tokenizer:
    """最小 BERT 风格 fixture：后处理器显式添加 [CLS]/[SEP]。"""

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


def _write_fixture_model_dir(tmp_path: Path, *, truncation_max_length: int | None = None) -> Path:
    model_dir = tmp_path / "bge-small-zh-v1.5"
    model_dir.mkdir()
    tokenizer = _fixture_tokenizer()
    if truncation_max_length is not None:
        tokenizer.enable_truncation(max_length=truncation_max_length)
    tokenizer.save(str(model_dir / "tokenizer.json"))
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


# ---------------------------------------------------------------------------
# 钉死摘要：backend 不得与 inference 身份漂移
# ---------------------------------------------------------------------------


def test_pinned_artifacts_cover_exactly_the_four_tokenizer_files() -> None:
    assert tuple(TOKENIZER_ARTIFACTS) == FIXTURE_FILES
    assert "config.json" not in TOKENIZER_ARTIFACTS
    assert "model.safetensors" not in TOKENIZER_ARTIFACTS


def test_pinned_artifacts_match_inference_identity_and_build_script() -> None:
    identity = _module_constant(INFERENCE_IDENTITY, "MODEL_ARTIFACT_DIGESTS")
    script = _module_constant(INFERENCE_SCRIPT, "EXPECTED_FILES")

    for name, (size, sha256) in TOKENIZER_ARTIFACTS.items():
        assert identity[name] == (size, sha256), name
        assert (script[name]["size"], script[name]["sha256"]) == (size, sha256), name


# ---------------------------------------------------------------------------
# 部署接线：tokenizers 只在 worker stage，产物由 inference 构建上下文提供
# ---------------------------------------------------------------------------


def test_worker_stage_installs_tokenizers_and_copies_only_four_artifacts() -> None:
    content = SHARED_DOCKERFILE.read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev --group worker" in content
    for name in FIXTURE_FILES:
        assert f"COPY --from=model_assets /models/bge-small-zh-v1.5/{name}" in content, name
    # worker 不做推理：只复制计数所需四件，api 之前的 runtime/api 段不得引用模型上下文。
    before_worker = content.split("FROM runtime AS worker")[0]
    assert "model_assets" not in before_worker
    assert "/models/bge-small-zh-v1.5/config.json" not in content
    assert "/models/bge-small-zh-v1.5/model.safetensors" not in content


def test_compose_provides_model_assets_context_from_inference() -> None:
    for path in (COMPOSE_FILE, QUEUE_COMPOSE_FILE):
        text = path.read_text(encoding="utf-8")
        assert "additional_contexts:" in text, path
        assert "model_assets: service:inference" in text, path


# ---------------------------------------------------------------------------
# 目录集合与逐字节校验
# ---------------------------------------------------------------------------


def test_verify_accepts_exact_matching_directory(tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)

    verify_tokenizer_directory(model_dir, expected=_artifact_table(model_dir))


def test_verify_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(TokenCounterError, match="不存在"):
        verify_tokenizer_directory(tmp_path / "absent", expected={})


def test_verify_rejects_missing_file(tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)
    (model_dir / "vocab.txt").unlink()

    with pytest.raises(TokenCounterError, match="缺失"):
        verify_tokenizer_directory(model_dir, expected=table)


def test_verify_rejects_extra_file(tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)
    (model_dir / "model.safetensors").write_bytes(b"not-a-weight")

    with pytest.raises(TokenCounterError, match="额外"):
        verify_tokenizer_directory(model_dir, expected=table)


def test_verify_rejects_size_mismatch(tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)
    (model_dir / "vocab.txt").write_bytes(b"short")

    with pytest.raises(TokenCounterError, match="大小不符"):
        verify_tokenizer_directory(model_dir, expected=table)


def test_verify_rejects_same_size_but_different_bytes(tmp_path: Path) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)
    original = (model_dir / "vocab.txt").read_bytes()
    tampered = bytes(byte ^ 0xFF for byte in original)
    assert len(tampered) == len(original)
    (model_dir / "vocab.txt").write_bytes(tampered)

    with pytest.raises(TokenCounterError, match="SHA-256 不符"):
        verify_tokenizer_directory(model_dir, expected=table)


def test_verify_wraps_directory_read_error_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)
    real_iterdir = Path.iterdir

    def failing_iterdir(self: Path) -> Iterator[Path]:
        if self == model_dir:
            raise PermissionError("/secret/model-directory")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)

    with pytest.raises(TokenCounterError, match="权限不足") as info:
        verify_tokenizer_directory(model_dir, expected=table)

    message = str(info.value)
    assert "/secret" not in message
    assert str(model_dir) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_verify_wraps_is_dir_oserror_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)
    real_is_dir = Path.is_dir

    def failing_is_dir(self: Path) -> bool:
        if self == model_dir:
            raise PermissionError("/secret/model-directory")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", failing_is_dir)

    with pytest.raises(TokenCounterError, match="权限不足") as info:
        verify_tokenizer_directory(model_dir, expected=table)

    message = str(info.value)
    assert "/secret" not in message
    assert str(model_dir) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("factory", "fragment"),
    [
        (lambda: PermissionError("/secret/artifact-path"), "权限不足"),
        (lambda: FileNotFoundError("/secret/artifact-path"), "文件不存在"),
    ],
)
def test_verify_wraps_artifact_read_oserror_without_leaking_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], OSError],
    fragment: str,
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    table = _artifact_table(model_dir)

    def failing_sha256(path: Path) -> str:
        raise factory()

    monkeypatch.setattr(token_counting, "sha256_file", failing_sha256)

    with pytest.raises(TokenCounterError, match=fragment) as info:
        verify_tokenizer_directory(model_dir, expected=table)

    message = str(info.value)
    assert "/secret" not in message
    assert str(model_dir) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# 构造路径：先校验、后加载；计数含特殊 token 且不截断
# ---------------------------------------------------------------------------


def test_counter_counts_special_tokens_and_does_not_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))

    counter = LocalTokenizerCounter(model_dir)

    # [CLS] hello world [SEP]
    assert counter.count_tokens("hello world") == 4
    # 空串仍含 [CLS] 与 [SEP]
    assert counter.count_tokens("") == 2
    # 未登录词映射到 [UNK]，仍带特殊 token
    assert counter.count_tokens("missing") == 3
    long_text = " ".join("hello" for _ in range(50))
    assert counter.count_tokens(long_text) == 52


def test_counter_ignores_truncation_configured_in_tokenizer_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fixture 的 tokenizer.json 自带 max_length=4 截断；计数器必须显式关闭它。
    model_dir = _write_fixture_model_dir(tmp_path, truncation_max_length=4)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))
    long_text = " ".join("hello" for _ in range(600))

    loaded = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    # sanity：不关闭截断时 600 个 token 会被压到 4，证明 fixture 确实带截断。
    assert len(loaded.encode(long_text, add_special_tokens=True).ids) == 4

    counter = LocalTokenizerCounter(model_dir)

    assert counter.count_tokens(long_text) == 602


def test_counter_calls_no_truncation_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))
    inner = _fixture_tokenizer()
    calls: list[str] = []

    class _SpyTokenizer:
        def no_truncation(self) -> _SpyTokenizer:
            calls.append("no_truncation")
            inner.no_truncation()
            return self

        def encode(self, text: str, add_special_tokens: bool = True) -> Encoding:
            return inner.encode(text, add_special_tokens=add_special_tokens)

    class _Factory:
        def from_file(self, path: str) -> _SpyTokenizer:
            return _SpyTokenizer()

    monkeypatch.setattr(token_counting, "Tokenizer", _Factory())

    counter = LocalTokenizerCounter(model_dir)

    assert calls == ["no_truncation"]
    assert counter.count_tokens("hello world") == 4


def test_counter_wraps_tokenizer_load_oserror_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))

    class _Factory:
        def from_file(self, path: str) -> object:
            raise FileNotFoundError("/secret/model/tokenizer.json")

    monkeypatch.setattr(token_counting, "Tokenizer", _Factory())

    with pytest.raises(TokenCounterError, match="文件不存在") as info:
        LocalTokenizerCounter(model_dir)

    message = str(info.value)
    assert "/secret" not in message
    assert str(model_dir) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_counter_wraps_tokenizer_parse_error_without_leaking_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    (model_dir / "tokenizer.json").write_bytes(b"not a tokenizer json")
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))

    with pytest.raises(TokenCounterError, match="解析失败") as info:
        LocalTokenizerCounter(model_dir)

    message = str(info.value)
    assert "not a tokenizer json" not in message
    assert str(model_dir) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_counter_verifies_every_artifact_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))
    # tokenizer.json 仍然有效；损坏非 tokenizer 文件也必须让构造失败，证明校验先于加载。
    (model_dir / "vocab.txt").write_bytes(b"tampered")

    with pytest.raises(TokenCounterError, match="tokenizer 文件"):
        LocalTokenizerCounter(model_dir)


def test_counter_reuses_one_tokenizer_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))

    counter = LocalTokenizerCounter(model_dir)
    # 删除磁盘上的产物后仍能计数，证明实例把 tokenizer 缓存在进程内而不是每次重读。
    (model_dir / "tokenizer.json").unlink()

    assert counter.count_tokens("hello world") == 4


def test_worker_counter_is_a_process_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = _write_fixture_model_dir(tmp_path)
    monkeypatch.setattr(token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(model_dir))
    monkeypatch.setattr(token_counting, "DEFAULT_MODEL_DIRECTORY", model_dir)
    token_counting.get_worker_token_counter.cache_clear()
    try:
        first = token_counting.get_worker_token_counter()
        second = token_counting.get_worker_token_counter()

        assert first is second
        assert first.count_tokens("hello world") == 4
    finally:
        token_counting.get_worker_token_counter.cache_clear()


# ---------------------------------------------------------------------------
# 真实产物 golden 对照（仅在已具备烘入产物时运行）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not DEFAULT_MODEL_DIRECTORY.is_dir(),
    reason="本机没有已烘入的 BGE tokenizer 产物；真实对照由独立 tester 在 worker 镜像内复核",
)
def test_real_tokenizer_counts_match_golden_reference() -> None:
    counter = LocalTokenizerCounter(DEFAULT_MODEL_DIRECTORY)
    golden = cast("dict[str, Any]", json.loads(GOLDEN_REFERENCE.read_text(encoding="utf-8")))

    samples = cast("list[dict[str, Any]]", golden["samples"])
    assert samples, "golden 参考必须包含样本"
    for sample in samples:
        assert counter.count_tokens(sample["text"]) == sample["token_count"], sample["id"]
