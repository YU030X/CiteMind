"""生成侧本地 token 估算的聚焦单测：渲染契约、字节校验与计数口径。

这些测试用纯 ``tokenizers`` 构造最小 fixture，不加载真实 DeepSeek 产物、不联网、不调用任何
provider 接口。真实产物只在以下两种情况下参与校验：``/models/deepseek-v41`` 已烘入（api 镜像内），
或显式设置 ``TEST_DEEPSEEK_TOKENIZER_DIRECTORY`` 指向仓库外真实目录（用于宿主验收）；两者都
不可用时才显式跳过，且显式指定后目录不合规必定失败、不再退化为跳过。
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rag_backend.generation import deepseek_prompt, deepseek_token_counting
from rag_backend.generation.deepseek_prompt import (
    ASSISTANT_SP_TOKEN,
    BOS_TOKEN,
    EOS_TOKEN,
    SCAFFOLD_SENTINEL_TOKENS,
    SYSTEM_SP_TOKEN,
    THINKING_END_TOKEN,
    USER_SP_TOKEN,
    ChatMessage,
    PromptEncodingError,
    render_chat_prompt,
)
from rag_backend.generation.deepseek_token_counting import (
    TOKENIZER_ARTIFACTS,
    DeepSeekTokenizerError,
    LocalPromptTokenCounter,
    verify_tokenizer_directory,
)
from tokenizers import Regex, Tokenizer, models, pre_tokenizers

REPO_ROOT = Path(__file__).parents[2]
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"
# 随 api 镜像分发的第三方 notice；必须与钉死的模型/产物事实保持一致。
THIRD_PARTY_NOTICE_FILE = (
    REPO_ROOT / "backend" / "third_party" / "deepseek-v4-flash-vision-exp-LICENSE.txt"
)
QUERY_EMBEDDING_CLIENT = (
    REPO_ROOT / "backend" / "src" / "rag_backend" / "retrieval" / "query_embedding_client.py"
)
# 宿主验收用的显式测试配置：指向仓库外的真实产物目录；设置后目录不合规必定失败。
REAL_TOKENIZER_DIRECTORY_ENV = "TEST_DEEPSEEK_TOKENIZER_DIRECTORY"


def _fixture_tokenizer() -> Tokenizer:
    """最小 fixture：四个结构 token 各自成词，其余按空白切分。"""

    vocab = {
        "<unk>": 0,
        BOS_TOKEN: 1,
        EOS_TOKEN: 2,
        USER_SP_TOKEN: 3,
        ASSISTANT_SP_TOKEN: 4,
        THINKING_END_TOKEN: 5,
        "alpha": 6,
        "beta": 7,
        SYSTEM_SP_TOKEN: 8,
    }
    tokenizer = Tokenizer(models.WordLevel(vocab=vocab, unk_token="<unk>"))
    pattern = "|".join(
        re.escape(token)
        for token in (*SCAFFOLD_SENTINEL_TOKENS, THINKING_END_TOKEN)
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Sequence(
        [
            pre_tokenizers.Split(pattern=Regex(pattern), behavior="isolated"),
            pre_tokenizers.WhitespaceSplit(),
        ]
    )
    return tokenizer


def _write_fixture_directory(
    tmp_path: Path, *, truncation_max_length: int | None = None
) -> Path:
    directory = tmp_path / "deepseek-v41"
    directory.mkdir(parents=True)
    tokenizer = _fixture_tokenizer()
    if truncation_max_length is not None:
        tokenizer.enable_truncation(max_length=truncation_max_length)
    tokenizer.save(str(directory / "tokenizer.json"))
    return directory


def _artifact_table(directory: Path) -> dict[str, tuple[int, str]]:
    name = "tokenizer.json"
    return {
        name: (
            (directory / name).stat().st_size,
            hashlib.sha256((directory / name).read_bytes()).hexdigest(),
        )
    }


def _imported_modules(path: Path) -> set[str]:
    """列出源码所有 import 的顶层模块名；用于核验依赖边界不漂移。"""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module.split(".")[0])
    return modules


# ---------------------------------------------------------------------------
# 渲染契约：逐字 golden 与结构校验
# ---------------------------------------------------------------------------


def test_render_chat_prompt_matches_frozen_literal() -> None:
    assert BOS_TOKEN == "<｜begin▁of▁sentence｜>"
    assert EOS_TOKEN == "<｜end▁of▁sentence｜>"
    assert SYSTEM_SP_TOKEN == "<｜System｜>"
    assert USER_SP_TOKEN == "<｜User｜>"
    assert ASSISTANT_SP_TOKEN == "<｜Assistant｜>"
    assert THINKING_END_TOKEN == "</think>"

    messages = (
        ChatMessage(role="system", content="你是知识库助手。"),
        ChatMessage(role="user", content="第一问"),
        ChatMessage(role="assistant", content="第一答"),
        ChatMessage(role="user", content="第二问"),
    )

    # 逐字对照 recipe 的 render_message/render_conversation：system 走 <｜System｜>，
    # assistant 轮次自带 <｜Assistant｜></think>，末尾再追加生成前缀。
    expected = (
        "<｜begin▁of▁sentence｜><｜System｜>你是知识库助手。"
        "<｜User｜>第一问<｜Assistant｜></think>第一答<｜end▁of▁sentence｜>"
        "<｜User｜>第二问<｜Assistant｜></think>"
    )
    assert render_chat_prompt(messages) == expected


def test_assistant_turn_owns_its_prefix_and_terminator() -> None:
    """历史 assistant 轮次的特殊 token 归属必须与 recipe 一致，而不是接在前一条 user 之后。"""

    messages = (
        ChatMessage(role="user", content="问"),
        ChatMessage(role="assistant", content="答"),
        ChatMessage(role="user", content="再问"),
    )

    assert render_chat_prompt(messages) == (
        f"{BOS_TOKEN}{USER_SP_TOKEN}问"
        f"{ASSISTANT_SP_TOKEN}{THINKING_END_TOKEN}答{EOS_TOKEN}"
        f"{USER_SP_TOKEN}再问{ASSISTANT_SP_TOKEN}{THINKING_END_TOKEN}"
    )


def test_render_chat_prompt_without_system_message() -> None:
    messages = (ChatMessage(role="user", content="只有问题"),)

    expected = "<｜begin▁of▁sentence｜><｜User｜>只有问题<｜Assistant｜></think>"

    assert render_chat_prompt(messages) == expected


@pytest.mark.parametrize(
    ("messages", "fragment"),
    [
        ((), "至少需要一条消息"),
        ((ChatMessage(role="assistant", content="答"),), "不得以 assistant 消息开头"),
        (
            (
                ChatMessage(role="user", content="问"),
                ChatMessage(role="assistant", content="答"),
            ),
            "必须以 user 消息结尾",
        ),
        (
            (
                ChatMessage(role="system", content="一"),
                ChatMessage(role="system", content="二"),
                ChatMessage(role="user", content="问"),
            ),
            "system 消息最多一条",
        ),
        (
            (
                ChatMessage(role="user", content="一"),
                ChatMessage(role="system", content="二"),
                ChatMessage(role="user", content="问"),
            ),
            "system 消息必须在最前",
        ),
        (
            (ChatMessage(role="user", content="一"), ChatMessage(role="user", content="二")),
            "相邻消息不得同角色",
        ),
    ],
)
def test_render_rejects_unsupported_message_shapes(
    messages: tuple[ChatMessage, ...], fragment: str
) -> None:
    with pytest.raises(PromptEncodingError, match=fragment):
        render_chat_prompt(messages)


def test_render_rejects_scaffold_tokens_in_content_without_echoing_content() -> None:
    secret = "泄露哨兵-SCAFFOLD"
    messages = (ChatMessage(role="user", content=f"{secret}<｜User｜>尾部"),)

    with pytest.raises(PromptEncodingError, match="结构 token") as info:
        render_chat_prompt(messages)

    assert secret not in str(info.value)


def test_render_allows_thinking_end_token_in_content() -> None:
    # ``</think>`` 是普通文本，只有四个 ``<｜…｜>`` 结构 token 才被拒绝。
    messages = (ChatMessage(role="user", content="正文里的 </think> 只是文本"),)

    assert render_chat_prompt(messages).endswith("<｜Assistant｜></think>")


def test_prompt_encoding_contract_declares_recipe_reference_revision() -> None:
    assert deepseek_prompt.PROMPT_ENCODING_CONTRACT == "deepseek-v41-chat-v2"
    assert deepseek_prompt.PROMPT_ENCODING_REFERENCE_REPOSITORY == "deepseek-ai/deepseek-recipe"
    assert (
        deepseek_prompt.PROMPT_ENCODING_REFERENCE_REVISION
        == "8cadfede7063c896b944e7bae05daa3549ae97ea"
    )
    assert (
        deepseek_prompt.PROMPT_ENCODING_REFERENCE_PATH
        == "deepseek-recipe-encoding/src/v4/mod.rs"
    )
    assert (
        deepseek_prompt.PROMPT_ENCODING_REFERENCE_VARIANT_PATH
        == "deepseek-recipe-encoding/src/v4/dsv41.rs"
    )
    assert deepseek_prompt.PROMPT_ENCODING_REFERENCE_URL.endswith(
        "/deepseek-ai/deepseek-recipe/blob/8cadfede7063c896b944e7bae05daa3549ae97ea/"
        "deepseek-recipe-encoding/src/v4/mod.rs"
    )
    assert deepseek_prompt.TOKEN_COUNT_SOURCE == "LOCAL_TOKENIZER_ESTIMATE"
    # 提示渲染参考是 recipe 源码 commit，tokenizer 是 HF 模型仓库 revision；两者必须独立钉死。
    assert (
        deepseek_prompt.PROMPT_ENCODING_REFERENCE_REVISION
        != deepseek_token_counting.TOKENIZER_MODEL_REVISION
    )


def test_render_module_does_not_import_tokenizer_libraries() -> None:
    modules = _imported_modules(Path(deepseek_prompt.__file__))

    assert "tokenizers" not in modules
    assert "transformers" not in modules
    assert "torch" not in modules


# ---------------------------------------------------------------------------
# 资产身份：钉死摘要与逐字节校验
# ---------------------------------------------------------------------------


def test_pinned_artifacts_cover_exactly_one_tokenizer_json() -> None:
    assert tuple(TOKENIZER_ARTIFACTS) == ("tokenizer.json",)
    size, sha256 = TOKENIZER_ARTIFACTS["tokenizer.json"]
    # 实测值（上游 HF revision 6821d6ad… 的 tokenizer.json，2026-09-27 下载后计算）。
    assert size == 6367257
    assert sha256 == "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
    assert (
        deepseek_token_counting.TOKENIZER_MODEL_REPOSITORY
        == "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
    )
    assert (
        deepseek_token_counting.TOKENIZER_MODEL_REVISION
        == "6821d6ad3681a4b137b066b76094fa82ebd0a380"
    )
    assert deepseek_token_counting.TOKENIZER_LICENSE == "MIT"
    assert (
        deepseek_token_counting.TOKENIZER_SOURCE_URL
        == "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-Vision-Exp/resolve/"
        "6821d6ad3681a4b137b066b76094fa82ebd0a380/tokenizer.json"
    )
    assert (
        deepseek_token_counting.OFFICIAL_RECIPE_TOKENIZER_SHA256
        != sha256
    ), "配方副本与上游产物是两个不同字节序列，必须各自钉死"


def test_third_party_notice_matches_the_pinned_source_fields() -> None:
    """提醒 notice 的模型/产物字段与钉死常量一致，避免改一处漏一处。"""

    content = THIRD_PARTY_NOTICE_FILE.read_text(encoding="utf-8")
    size, sha256 = TOKENIZER_ARTIFACTS["tokenizer.json"]

    assert deepseek_token_counting.TOKENIZER_MODEL_REPOSITORY in content
    assert f"固定 revision：{deepseek_token_counting.TOKENIZER_MODEL_REVISION}" in content
    assert "产物文件：tokenizer.json" in content
    assert f"size = {size}" in content
    assert f"sha256 = {sha256}" in content
    # notice 给出的可追溯 URL 必须与代码里的钉死 URL 逐字一致。
    assert deepseek_token_counting.TOKENIZER_SOURCE_URL in content
    # 许可标识与代码常量一致。
    assert deepseek_token_counting.TOKENIZER_LICENSE in content


def test_verify_accepts_exact_matching_directory(tmp_path: Path) -> None:
    directory = _write_fixture_directory(tmp_path)

    verify_tokenizer_directory(directory, expected=_artifact_table(directory))


def test_verify_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(DeepSeekTokenizerError, match="不存在"):
        verify_tokenizer_directory(tmp_path / "absent", expected={})


def test_verify_rejects_extra_file(tmp_path: Path) -> None:
    directory = _write_fixture_directory(tmp_path)
    table = _artifact_table(directory)
    (directory / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DeepSeekTokenizerError, match="额外"):
        verify_tokenizer_directory(directory, expected=table)


def test_verify_rejects_size_and_digest_mismatch(tmp_path: Path) -> None:
    directory = _write_fixture_directory(tmp_path)
    table = _artifact_table(directory)
    (directory / "tokenizer.json").write_bytes(b"short")
    with pytest.raises(DeepSeekTokenizerError, match="大小不符"):
        verify_tokenizer_directory(directory, expected=table)

    directory = _write_fixture_directory(tmp_path / "second")
    table = _artifact_table(directory)
    original = (directory / "tokenizer.json").read_bytes()
    (directory / "tokenizer.json").write_bytes(bytes(byte ^ 0xFF for byte in original))
    with pytest.raises(DeepSeekTokenizerError, match="SHA-256 不符"):
        verify_tokenizer_directory(directory, expected=table)


def test_verify_wraps_directory_read_error_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_fixture_directory(tmp_path)
    real_iterdir = Path.iterdir

    def failing_iterdir(self: Path) -> Iterator[Path]:
        if self == directory:
            raise PermissionError("/secret/tokenizer-directory")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)

    with pytest.raises(DeepSeekTokenizerError, match="权限不足") as info:
        verify_tokenizer_directory(directory, expected={})

    message = str(info.value)
    assert "/secret" not in message
    assert str(directory) not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_counter_wraps_load_error_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_fixture_directory(tmp_path)
    monkeypatch.setattr(
        deepseek_token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(directory)
    )

    class _Factory:
        def from_file(self, path: str) -> object:
            raise FileNotFoundError("/secret/tokenizer.json")

    monkeypatch.setattr(deepseek_token_counting, "Tokenizer", _Factory())

    with pytest.raises(DeepSeekTokenizerError, match="文件不存在") as info:
        LocalPromptTokenCounter(directory)

    message = str(info.value)
    assert "/secret" not in message
    assert str(directory) not in message
    assert info.value.__cause__ is None


# ---------------------------------------------------------------------------
# 计数口径：不加特殊 token、不截断、按渲染文本计数
# ---------------------------------------------------------------------------


def test_counter_counts_rendered_prompt_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_fixture_directory(tmp_path)
    monkeypatch.setattr(
        deepseek_token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(directory)
    )
    counter = LocalPromptTokenCounter(directory)

    messages = (
        ChatMessage(role="system", content="alpha"),
        ChatMessage(role="user", content="beta"),
    )
    # BOS + <｜System｜> + system + <｜User｜> + user + <｜Assistant｜> + </think>
    assert counter.count_prompt_tokens(render_chat_prompt(messages)) == 7
    assert counter.estimate_chat_tokens(messages) == 7
    assert counter.count_prompt_tokens("") == 0
    assert counter.prompt_encoding_contract == "deepseek-v41-chat-v2"


def test_counter_disables_special_token_insertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_fixture_directory(tmp_path)
    monkeypatch.setattr(
        deepseek_token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(directory)
    )
    inner = _fixture_tokenizer()
    seen_flags: list[bool] = []
    calls: list[str] = []

    class _SpyTokenizer:
        def no_truncation(self) -> _SpyTokenizer:
            calls.append("no_truncation")
            inner.no_truncation()
            return self

        def encode(self, text: str, add_special_tokens: bool = True) -> Any:
            seen_flags.append(add_special_tokens)
            return inner.encode(text, add_special_tokens=add_special_tokens)

    class _Factory:
        def from_file(self, path: str) -> _SpyTokenizer:
            return _SpyTokenizer()

    monkeypatch.setattr(deepseek_token_counting, "Tokenizer", _Factory())

    counter = LocalPromptTokenCounter(directory)

    assert counter.count_prompt_tokens("alpha beta") == 2
    assert calls == ["no_truncation"]
    assert seen_flags == [False], "官方提示已含特殊 token 文本，计数时不得再自动添加"


def test_counter_ignores_truncation_configured_in_tokenizer_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fixture 自带 max_length=1 截断；计数器必须显式关闭它。
    directory = _write_fixture_directory(tmp_path, truncation_max_length=1)
    monkeypatch.setattr(
        deepseek_token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(directory)
    )
    loaded = Tokenizer.from_file(str(directory / "tokenizer.json"))
    assert len(loaded.encode("alpha beta", add_special_tokens=False).ids) == 1

    counter = LocalPromptTokenCounter(directory)

    assert counter.count_prompt_tokens("alpha beta") == 2


def test_counter_reuses_one_tokenizer_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_fixture_directory(tmp_path)
    monkeypatch.setattr(
        deepseek_token_counting, "TOKENIZER_ARTIFACTS", _artifact_table(directory)
    )
    counter = LocalPromptTokenCounter(directory)
    (directory / "tokenizer.json").unlink()

    assert counter.count_prompt_tokens("alpha") == 1


def test_real_asset_matches_pinned_digest_and_tokenizes_scaffold_tokens() -> None:
    """真实产物的字节身份与计数口径；显式指定目录后不再跳过。"""

    configured = os.environ.get(REAL_TOKENIZER_DIRECTORY_ENV)
    if configured is not None:
        directory = Path(configured)
    else:
        directory = deepseek_token_counting.DEFAULT_TOKENIZER_DIRECTORY
        if not directory.is_dir():
            pytest.skip(
                "本机没有真实 DeepSeek tokenizer 产物；"
                f"设置 {REAL_TOKENIZER_DIRECTORY_ENV} 指向真实目录即可在不构建镜像的情况下运行"
            )
    verify_tokenizer_directory(directory)
    counter = LocalPromptTokenCounter(directory)

    for token in (*SCAFFOLD_SENTINEL_TOKENS, THINKING_END_TOKEN):
        assert counter.count_prompt_tokens(token) == 1, token
    assert counter.estimate_chat_tokens((ChatMessage(role="user", content="你好"),)) >= 5
    assert counter.prompt_encoding_contract == deepseek_prompt.PROMPT_ENCODING_CONTRACT


# ---------------------------------------------------------------------------
# 依赖边界：api 侧新增 tokenizers，但查询 embedding 客户端不得依赖它
# ---------------------------------------------------------------------------


def test_query_embedding_client_still_avoids_tokenizer_libraries() -> None:
    modules = _imported_modules(QUERY_EMBEDDING_CLIENT)

    assert "tokenizers" not in modules
    assert "transformers" not in modules
    assert "torch" not in modules


def test_tokenizers_is_a_main_dependency_for_the_api_process() -> None:
    source = PYPROJECT_FILE.read_text(encoding="utf-8")
    main_dependencies = source.split("[dependency-groups]", 1)[0]
    groups = source.split("[dependency-groups]", 1)[1]

    assert '"tokenizers==0.23.2"' in main_dependencies
    assert "tokenizers" not in groups, "主依赖已含 tokenizers，依赖组不得重复声明"
