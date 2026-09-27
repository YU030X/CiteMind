"""API 侧本地 token 估算：只从已烘入镜像的 DeepSeek V4.1 tokenizer 产物加载并逐字节校验。

本模块只做纯本地计算：不联网、不读环境密钥、不把提示文本写进日志、不接触数据库、Redis 或
Celery。它实现 :class:`~rag_backend.generation.deepseek_prompt.PromptTokenEstimator`，用
**DeepSeek 自己的** tokenizer 计数本地渲染的 chat 提示——绝不复用 worker 侧 BGE 计数
（`chunk.token_count` 是另一个模型的词表口径，两者不可互换）。

资产身份固定：`deepseek-ai/DeepSeek-V4-Flash-Vision-Exp` 的 ``tokenizer.json``，revision
``6821d6ad3681a4b137b066b76094fa82ebd0a380``，MIT 许可；可追溯 URL 见
:data:`TOKENIZER_SOURCE_URL`。这是 HF **模型仓库** 的 revision；提示渲染契约依据的 recipe **源码**
revision（``deepseek-ai/deepseek-recipe``）是另一个独立事实，本模块刻意不 import 后者、也不用其中
一个代替另一个。本仓库不下载、不编译任何模型代码：只按 :data:`TOKENIZER_ARTIFACTS` 钉死的字节大小
与 SHA-256 校验目录内**恰好一个**产物文件，再用 ``tokenizers.Tokenizer.from_file`` 离线加载，因此
不使用 ``transformers``，也不使用 ``trust_remote_code``。

计数口径按官方说明固定为 ``add_special_tokens=False``：官方提示文本本身已经包含 BOS/EOS 等
特殊 token 字面量。计数结果一律是**本地估算**（:data:`TOKEN_COUNT_SOURCE` 为
``LOCAL_TOKENIZER_ESTIMATE``），不是 provider 精确用量；官方以响应 ``usage`` 为准，且官方
仓库已知 issue 显示正文含特殊 token 字面量时本地计数会与 provider 上报值不一致，因此调用方
必须把真实用量另按 provider 响应记账。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from tokenizers import Tokenizer

from rag_backend.generation.deepseek_prompt import (
    NON_THINKING,
    PROMPT_ENCODING_CONTRACT,
    ChatMessage,
    ThinkingChoice,
    render_chat_prompt,
)

# api 镜像内烘入路径；目录内只允许出现下表的产物，多一个少一个都视为不可信。
DEFAULT_TOKENIZER_DIRECTORY: Final = Path("/models/deepseek-v41")

TOKENIZER_MODEL_REPOSITORY: Final = "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
# HF 模型仓库 revision，只标识 tokenizer 产物来源；与 recipe 源码 revision 无关。
TOKENIZER_MODEL_REVISION: Final = "6821d6ad3681a4b137b066b76094fa82ebd0a380"
TOKENIZER_LICENSE: Final = "MIT"
TOKENIZER_SOURCE_URL: Final = (
    f"https://huggingface.co/{TOKENIZER_MODEL_REPOSITORY}/resolve/"
    f"{TOKENIZER_MODEL_REVISION}/tokenizer.json"
)
# 本模块实测（2026-09-27，宿主机下载该 HF revision 的 tokenizer.json 后计算）：
#   size=6367257, sha256=c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b
# 交叉证据（只用于核对，不作为本模块资产）：recipe 仓库内置的 v41 副本是同一上游 revision 的修改
# 副本，只把 id 129264 的 image token 由 <｜deepseek_image｜> 改为 <｜image｜>，对纯文本计数无影响；
# 实测 size=6367247、sha256=81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99，与其
# 公布的 static/tokenizers/v41/README.md（https://github.com/deepseek-ai/deepseek-recipe/blob/main/static/tokenizers/v41/README.md）
# 逐字一致。两者是不同字节序列，必须各自钉死、不得互相代替。
TOKENIZER_ARTIFACTS: Final[dict[str, tuple[int, str]]] = {
    "tokenizer.json": (
        6367257,
        "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
    ),
}
OFFICIAL_RECIPE_TOKENIZER_SHA256: Final = (
    "81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99"
)


class DeepSeekTokenizerError(RuntimeError):
    """tokenizer 产物缺失、文件集合不符、字节摘要不符或加载失败；调用方应转启动失败。"""


def _io_failure_reason(error: OSError) -> str:
    """把 OSError 类型映射为静态原因，绝不回显异常文本里的外部路径。"""

    if isinstance(error, FileNotFoundError):
        return "文件不存在"
    if isinstance(error, PermissionError):
        return "权限不足"
    if isinstance(error, IsADirectoryError):
        return "不是常规文件"
    return "IO 错误"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_tokenizer_directory(
    tokenizer_directory: Path,
    expected: Mapping[str, tuple[int, str]] | None = None,
) -> None:
    """核验目录内恰为 ``expected`` 声明的文件集合，且逐文件大小与 SHA-256 一致。

    ``expected`` 省略时使用钉死的 :data:`TOKENIZER_ARTIFACTS`；显式传入只用于测试注入。
    所有 IO 失败都归一为静态 :class:`DeepSeekTokenizerError`，消息只含钉死产物名与错误类别，
    不含外部路径或产物正文。
    """

    table = TOKENIZER_ARTIFACTS if expected is None else expected
    try:
        if not tokenizer_directory.is_dir():
            raise DeepSeekTokenizerError("tokenizer 产物目录不存在")
        actual = {entry.name for entry in tokenizer_directory.iterdir()}
    except DeepSeekTokenizerError:
        raise
    except OSError as error:
        raise DeepSeekTokenizerError(
            f"tokenizer 产物目录不可读（{_io_failure_reason(error)}）"
        ) from None
    expected_names = set(table)
    extra = sorted(actual - expected_names)
    missing = sorted(expected_names - actual)
    if extra or missing:
        # 只报数量：目录里的文件名可能来自外部，不回显到错误消息里。
        raise DeepSeekTokenizerError(
            f"tokenizer 产物目录文件集合不符；缺失 {len(missing)} 个，额外 {len(extra)} 个"
        )
    for name, (size, sha256) in table.items():
        path = tokenizer_directory / name
        try:
            actual_size = path.stat().st_size
            actual_sha256 = sha256_file(path)
        except OSError as error:
            raise DeepSeekTokenizerError(
                f"tokenizer 产物不可读：{name}（{_io_failure_reason(error)}）"
            ) from None
        if actual_size != size:
            raise DeepSeekTokenizerError(f"tokenizer 产物大小不符：{name}")
        if actual_sha256 != sha256:
            raise DeepSeekTokenizerError(f"tokenizer 产物 SHA-256 不符：{name}")


class LocalPromptTokenCounter:
    """单个 API 进程内复用的本地估算器；构造时先校验字节再加载。

    构造顺序固定为先校验、后加载：任何损坏产物都必须在校验阶段以
    :class:`DeepSeekTokenizerError` 失败，而不是等到 ``tokenizers`` 解析时才报底层错误。
    加载后显式 ``no_truncation()``，即使 ``tokenizer.json`` 自带截断配置也按完整提示计数；
    底层 IO/解析异常统一归一为静态 :class:`DeepSeekTokenizerError`，不保留含路径的异常链。
    """

    def __init__(self, tokenizer_directory: Path = DEFAULT_TOKENIZER_DIRECTORY) -> None:
        verify_tokenizer_directory(tokenizer_directory)
        try:
            tokenizer = Tokenizer.from_file(str(tokenizer_directory / "tokenizer.json"))
            tokenizer.no_truncation()
        except OSError as error:
            raise DeepSeekTokenizerError(
                f"tokenizer 产物加载失败（{_io_failure_reason(error)}）"
            ) from None
        except Exception:
            raise DeepSeekTokenizerError("tokenizer 产物解析失败") from None
        self._tokenizer = tokenizer

    @property
    def prompt_encoding_contract(self) -> str:
        """本计数器使用的本地渲染契约版本。"""

        return PROMPT_ENCODING_CONTRACT

    def count_prompt_tokens(self, prompt: str) -> int:
        """返回提示文本的完整 token 数；不做截断，也不自动添加特殊 token。

        ``add_special_tokens=False`` 是官方口径：官方提示文本已含特殊 token 字面量，自动添加会
        重复计数。
        """

        return len(self._tokenizer.encode(prompt, add_special_tokens=False).ids)

    def estimate_chat_tokens(
        self, messages: Sequence[ChatMessage], *, thinking: ThinkingChoice = NON_THINKING
    ) -> int:
        """按给定思考选项渲染 chat 消息并返回 token 估算值；渲染不合法时抛异常。"""

        return self.count_prompt_tokens(render_chat_prompt(messages, thinking=thinking))


__all__ = [
    "DEFAULT_TOKENIZER_DIRECTORY",
    "OFFICIAL_RECIPE_TOKENIZER_SHA256",
    "TOKENIZER_ARTIFACTS",
    "TOKENIZER_LICENSE",
    "TOKENIZER_MODEL_REPOSITORY",
    "TOKENIZER_MODEL_REVISION",
    "TOKENIZER_SOURCE_URL",
    "DeepSeekTokenizerError",
    "LocalPromptTokenCounter",
    "sha256_file",
    "verify_tokenizer_directory",
]
