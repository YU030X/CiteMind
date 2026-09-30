"""解析子进程的有界输入/输出、超时终止与凭据隔离的纯本地测试。

超时与失败路径用假 ``Popen`` 覆盖，不需要真的挂起；一条真实子进程用例验证入口与父进程
解析结果一致。绝不连接数据库、Redis 或 inference。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import types
from typing import Any

import pytest
from docx_samples import (
    corrupted_zip_docx,
    nested_table_docx,
    positive_simple_table,
)
from pdf_samples import (
    all_blank_pdf,
    corrupt_pdf,
    encrypted_pdf,
    too_many_pages_pdf,
)
from pdf_samples import (
    positive_samples as pdf_positive_samples,
)
from rag_backend.ingestion import parse_subprocess as ps
from rag_backend.ingestion.chunking import ChunkBudget, chunk_markdown
from rag_backend.ingestion.docx_parsing import parse_docx
from rag_backend.ingestion.parsing import parse_markdown
from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION, parse_pdf
from rag_backend.ingestion.validation import MAX_DOCUMENT_BYTES, MAX_MARKDOWN_BYTES
from test_pdf_parsing import CharacterCounter, _build_pdf


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


def test_real_pdf_subprocess_parse_matches_direct() -> None:
    content = _build_pdf(["Subprocess page one", "Subprocess page two"])

    parsed = ps.parse_pdf_in_subprocess(content)

    expected = parse_pdf(content)
    assert parsed.source_sha256 == expected.source_sha256
    assert parsed.parser_version == expected.parser_version
    assert parsed.source_type == "pdf"
    assert [(b.text, b.page, b.heading_path) for b in parsed.blocks] == [
        (b.text, b.page, b.heading_path) for b in expected.blocks
    ]
    assert all(b.start_line is None and b.end_line is None for b in parsed.blocks)


def test_pdf_subprocess_extracts_all_positive_samples_to_locator_v2() -> None:
    """5 份自制正样本都走真实 PDF 解析子进程，并可切出 locator_version=2 的页定位。"""

    samples = pdf_positive_samples()
    assert len(samples) == 5
    for name, raw in samples.items():
        parsed = ps.parse_pdf_in_subprocess(raw)
        assert parsed.source_type == "pdf", name
        assert parsed.parser_version == PDF_PARSER_VERSION, name
        assert parsed.source_sha256 == hashlib.sha256(raw).hexdigest(), name
        chunks = chunk_markdown(
            parsed,
            CharacterCounter(),
            ChunkBudget(target_tokens=100, overlap_tokens=0, max_tokens=200),
        )
        assert chunks, name
        for chunk in chunks:
            assert chunk.source_locator["locator_version"] == 2, name
            assert chunk.source_locator["source_type"] == "pdf", name
            pages = chunk.source_locator["pages"]
            assert isinstance(pages, list) and len(pages) == 1, name


def test_pdf_subprocess_named_failures_are_static() -> None:
    """真实子进程把加密/损坏/超页分别映射为可区分的具名静态失败。"""

    with pytest.raises(ps.PdfEncryptedSubprocessError):
        ps.parse_pdf_in_subprocess(encrypted_pdf())
    with pytest.raises(ps.PdfInvalidSubprocessError):
        ps.parse_pdf_in_subprocess(corrupt_pdf())
    with pytest.raises(ps.PdfTooManyPagesSubprocessError):
        ps.parse_pdf_in_subprocess(too_many_pages_pdf())
    # 全空白页不是失败：子进程正常返回空块序列，由上层判 NEEDS_OCR。
    assert ps.parse_pdf_in_subprocess(all_blank_pdf()).blocks == ()


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (ps.EXIT_PDF_ENCRYPTED, ps.PdfEncryptedSubprocessError),
        (ps.EXIT_PDF_TOO_MANY_PAGES, ps.PdfTooManyPagesSubprocessError),
        (ps.EXIT_PDF_INVALID, ps.PdfInvalidSubprocessError),
    ],
)
def test_pdf_named_exit_codes_map_to_static_errors(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: type[Exception]
) -> None:
    process = _FakeProcess(returncode=returncode)
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(expected):
        ps.parse_pdf_in_subprocess(b"%PDF-1.4")


def test_pdf_oversized_input_is_rejected_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("超限输入不得启动子进程")

    monkeypatch.setattr(subprocess, "Popen", explode)

    with pytest.raises(ps.ParseSubprocessFailed):
        ps.parse_pdf_in_subprocess(b"x" * (MAX_DOCUMENT_BYTES + 1))


def test_unknown_source_argument_is_rejected() -> None:
    assert ps.main(["html"]) == ps.EXIT_INVALID_INPUT


def test_real_docx_subprocess_parse_matches_direct() -> None:
    content = positive_simple_table()

    parsed = ps.parse_docx_in_subprocess(content)

    expected = parse_docx(content)
    assert parsed.source_sha256 == expected.source_sha256
    assert parsed.parser_version == expected.parser_version
    assert parsed.source_type == "docx"
    assert [(b.kind, b.text, b.table_index, b.row_index) for b in parsed.blocks] == [
        (b.kind, b.text, b.table_index, b.row_index) for b in expected.blocks
    ]
    assert parsed.blocks[1].cells == expected.blocks[1].cells


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (ps.EXIT_DOCX_UNSUPPORTED, ps.DocxUnsupportedSubprocessError),
        (ps.EXIT_DOCX_INVALID, ps.DocxInvalidSubprocessError),
    ],
)
def test_docx_named_exit_codes_map_to_static_errors(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: type[Exception]
) -> None:
    process = _FakeProcess(returncode=returncode)
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(expected):
        ps.parse_docx_in_subprocess(positive_simple_table())


def test_real_docx_nested_table_fails_statically() -> None:
    with pytest.raises(ps.DocxUnsupportedSubprocessError):
        ps.parse_docx_in_subprocess(nested_table_docx())


def test_real_docx_corrupted_zip_fails_statically() -> None:
    with pytest.raises(ps.DocxInvalidSubprocessError):
        ps.parse_docx_in_subprocess(corrupted_zip_docx())


def test_docx_oversized_input_is_rejected_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("超限输入不得启动子进程")

    monkeypatch.setattr(subprocess, "Popen", explode)

    with pytest.raises(ps.ParseSubprocessFailed):
        ps.parse_docx_in_subprocess(b"x" * (MAX_DOCUMENT_BYTES + 1))


def test_child_memory_limit_is_noop_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert ps._apply_child_memory_limit() is None


def test_child_memory_limit_sets_finite_hard_limit_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    fake_resource = types.SimpleNamespace(
        RLIMIT_AS=9,
        setrlimit=lambda which, limits: calls.append((which, limits)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(sys, "platform", "linux")

    assert ps._apply_child_memory_limit() is None
    assert calls == [(9, (ps.PARSE_MEMORY_LIMIT_BYTES, ps.PARSE_MEMORY_LIMIT_BYTES))]


def test_child_memory_limit_setting_failure_is_static(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_which: int, _limits: tuple[int, int]) -> None:
        raise OSError("setrlimit denied")

    fake_resource = types.SimpleNamespace(RLIMIT_AS=9, setrlimit=denied)
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(sys, "platform", "linux")

    assert ps._apply_child_memory_limit() == ps.EXIT_MEMORY_LIMIT_UNAVAILABLE


def test_main_fails_statically_when_memory_limit_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ps, "_apply_child_memory_limit", lambda: ps.EXIT_MEMORY_LIMIT_UNAVAILABLE
    )

    assert ps.main([ps.SOURCE_TYPE_MARKDOWN]) == ps.EXIT_MEMORY_LIMIT_UNAVAILABLE


def test_memory_limit_exit_code_maps_to_static_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(returncode=ps.EXIT_MEMORY_LIMIT_UNAVAILABLE)
    _install_fake_popen(monkeypatch, process)

    with pytest.raises(ps.ParseMemoryLimitSubprocessError):
        ps.parse_markdown_in_subprocess(b"# T\n\nbody\n")


@pytest.mark.skipif(
    sys.platform != "linux", reason="RLIMIT_AS 硬内存上限仅 Linux 可用"
)
def test_linux_child_enforces_finite_address_space_limit() -> None:
    """真实 Linux 子进程应用上限后无法再分配超过上限的地址空间。"""

    code = (
        "import resource\n"
        "from rag_backend.ingestion import parse_subprocess as ps\n"
        "assert ps._apply_child_memory_limit() is None\n"
        "soft, hard = resource.getrlimit(resource.RLIMIT_AS)\n"
        "assert soft == ps.PARSE_MEMORY_LIMIT_BYTES == hard\n"
        "try:\n"
        "    bytearray(ps.PARSE_MEMORY_LIMIT_BYTES * 2)\n"
        "except MemoryError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=60
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
