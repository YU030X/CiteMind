"""解析子进程的有界输入/输出、超时终止与凭据隔离的纯本地测试。

超时与失败路径用假 ``Popen`` 覆盖，不需要真的挂起；一条真实子进程用例验证入口与父进程
解析结果一致。绝不连接数据库、Redis 或 inference。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from rag_backend.ingestion import parse_subprocess as ps
from rag_backend.ingestion.parsing import parse_markdown
from rag_backend.ingestion.validation import MAX_MARKDOWN_BYTES


class _FakeProcess:
    """最小 Popen 替身：可控超时、退出码与输出，记录 kill/wait 调用。"""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.returncode = returncode
        self.killed = False
        self.wait_calls = 0
        self._timeout = timeout
        self._stdout = stdout

    def communicate(self, input: Any = None, timeout: Any = None) -> tuple[bytes, None]:
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="parse", timeout=timeout)
        return (self._stdout, None)

    def poll(self) -> int | None:
        if self.killed:
            return -9
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: Any = None) -> int:
        self.wait_calls += 1
        return self.returncode


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)


def test_sanitized_environment_strips_credentials_and_keeps_minimum() -> None:
    env = ps.sanitized_environment(
        {
            "PATH": "/usr/bin",
            "SYSTEMROOT": r"C:\Windows",
            "DATABASE_URL": "postgresql://secret",
            "REDIS_URL": "redis://secret",
            "INFERENCE_TOKEN": "token",
            "LLM_API_KEY": "key",
        }
    )

    assert env == {"PATH": "/usr/bin", "SYSTEMROOT": r"C:\Windows"}
    assert not any("secret" in value or "token" in value.lower() for value in env.values())


def test_real_subprocess_parse_matches_direct_parse() -> None:
    content = "# 标题\n\n正文段落。\n\n- 列表项\n".encode()

    parsed = ps.parse_markdown_in_subprocess(content)

    expected = parse_markdown(content)
    assert parsed.source_sha256 == expected.source_sha256
    assert parsed.parser_version == expected.parser_version
    assert [(b.ordinal, b.kind, b.text, b.heading_path) for b in parsed.blocks] == [
        (b.ordinal, b.kind, b.text, b.heading_path) for b in expected.blocks
    ]


def test_timeout_kills_reaps_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(timeout=True)
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(ps.ParseSubprocessTimeout):
        ps.parse_markdown_in_subprocess(b"# T\n\nbody\n", timeout_seconds=1)

    assert process.killed is True


def test_non_zero_exit_is_static_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(returncode=ps.EXIT_INVALID_INPUT)
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(ps.ParseSubprocessFailed):
        ps.parse_markdown_in_subprocess(b"# T\n\nbody\n")


def test_oversized_result_is_static_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(stdout=b"x" * (ps.MAX_PARSE_RESULT_BYTES + 1))
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(ps.ParseSubprocessFailed):
        ps.parse_markdown_in_subprocess(b"# T\n\nbody\n")


def test_invalid_json_result_is_static_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(stdout=b"not-json")
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(ps.ParseSubprocessFailed):
        ps.parse_markdown_in_subprocess(b"# T\n\nbody\n")


def test_oversized_input_is_rejected_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("超限输入不得启动子进程")

    monkeypatch.setattr(subprocess, "Popen", explode)

    with pytest.raises(ps.ParseSubprocessFailed):
        ps.parse_markdown_in_subprocess(b"x" * (MAX_MARKDOWN_BYTES + 1))


def test_non_positive_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess()
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(ValueError):
        ps.parse_markdown_in_subprocess(b"# T\n\nbody\n", timeout_seconds=0)


def test_deserialize_rejects_malformed_structure() -> None:
    with pytest.raises(ps.ParseSubprocessFailed):
        ps._deserialize({"source_sha256": "x"}, text="body")
    with pytest.raises(ps.ParseSubprocessFailed):
        ps._deserialize({"source_sha256": "x", "parser_version": "v", "blocks": "no"}, text="b")
