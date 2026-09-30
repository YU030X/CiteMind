"""备份/隔离恢复入口的纯离线测试。

全部用例不连接 Docker、PostgreSQL、Redis 或网络，也不实际执行任何 `pg_dump`/`pg_restore`：
外部命令由假的 `CommandRunner` 代替，只在最后用本地 `sys.executable` 验证二进制 stdout 与超时
回收。覆盖默认 dry-run 零 IO、目标守卫、同源拒绝、不覆盖与失败清理、清单篡改拒绝、二进制转储
以及 schema/文件关联核对。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from rag_backend.operations import backup as bk

FIXED_NOW = datetime(2026, 9, 30, 6, 0, 0, tzinfo=UTC)
CONTAINER_ID = "a" * 64
IMAGE = "pgvector/pgvector:pg17@sha256:deadbeef"


# ---------------------------------------------------------------------------
# 假命令执行器与数据桩
# ---------------------------------------------------------------------------


@dataclass
class DbStub:
    revision: str = "20260929_0016"
    table_count: int = 18
    file_refs: tuple[str, ...] = ()
    ready_versions: int = 0
    ready_generations: int = 0
    chunk_rows: int = 0
    embedding_rows: int = 0
    active_mismatches: int = 0
    jobs_missing_generation: int = 0
    database_exists: bool = True
    target_table_count: int = 0


@dataclass(frozen=True)
class _FakeStat:
    """注入用最小 stat 替身：只提供代码读取的 st_mode/st_size。"""

    st_mode: int
    st_size: int = 0


class FakeRunner:
    """记录 argv 并按 responder 返回结果；stdout_path 时把字节写入文件。"""

    def __init__(self, responder: Callable[[list[str]], bk.CommandResult]) -> None:
        self.responder = responder
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        input_path: Path | None = None,
        stdout_path: Path | None = None,
        timeout_seconds: float,
        check: bool = True,
    ) -> bk.CommandResult:
        self.calls.append(list(argv))
        result = self.responder(list(argv))
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_bytes(result.stdout)
        if check and result.returncode != 0:
            raise bk.CommandError("fake", returncode=result.returncode)
        return result


def _psql_database(argv: list[str]) -> str:
    return next(a.split("=", 1)[1] for a in argv if a.startswith("--dbname="))


def _psql_query(argv: list[str]) -> str:
    return next(a.split("=", 1)[1] for a in argv if a.startswith("--command="))


def make_responder(
    stub: DbStub,
    *,
    target_database: str = "citemind_test",
    dump: bytes = b"\x00\x01DUMP\xff",
    export_tar: bytes = b"",
    volume_exists: bool = True,
    volume_empty: bool = True,
    fail_pg_restore: bool = False,
    inspect_fails: bool = False,
) -> Callable[[list[str]], bk.CommandResult]:
    def respond(argv: list[str]) -> bk.CommandResult:
        tool = argv[1] if len(argv) > 1 else ""
        if tool == "ps":
            return bk.CommandResult(0, CONTAINER_ID.encode(), b"")
        if tool == "inspect":
            if inspect_fails:
                return bk.CommandResult(1, b"", b"no such container")
            return bk.CommandResult(0, IMAGE.encode(), b"")
        if tool == "volume":
            return bk.CommandResult(0 if volume_exists else 1, b"[]", b"")
        if tool == "exec":
            if "pg_dump" in argv:
                return bk.CommandResult(0, dump, b"")
            if "pg_restore" in argv:
                return bk.CommandResult(1 if fail_pg_restore else 0, b"", b"boom")
            if "psql" in argv:
                return _respond_psql(argv, stub, target_database)
        if tool == "run":
            if "find" in argv:
                return bk.CommandResult(0, b"" if volume_empty else b"/src/x\n", b"")
            if "tar" in argv and "-cf" in argv:
                return bk.CommandResult(0, export_tar, b"")
            if "tar" in argv and "-xf" in argv:
                return bk.CommandResult(0, b"", b"")
        return bk.CommandResult(0, b"", b"")

    return respond


def _respond_psql(argv: list[str], stub: DbStub, target_database: str) -> bk.CommandResult:
    database = _psql_database(argv)
    query = _psql_query(argv)
    if "FROM pg_database" in query:
        value = "1" if stub.database_exists else ""
        return bk.CommandResult(0, (value + "\n").encode(), b"")
    if "information_schema.tables" in query:
        count = stub.target_table_count if database == target_database else stub.table_count
        return bk.CommandResult(0, f"{count}\n".encode(), b"")
    if "FROM alembic_version" in query:
        return bk.CommandResult(0, (stub.revision + "\n").encode(), b"")
    if "FROM document_version WHERE file_ref" in query:
        return bk.CommandResult(
            0, "".join(f"{ref}\n" for ref in stub.file_refs).encode(), b""
        )
    if "FROM document_version WHERE status" in query:
        return bk.CommandResult(0, f"{stub.ready_versions}\n".encode(), b"")
    if "FROM index_generation WHERE status" in query:
        return bk.CommandResult(0, f"{stub.ready_generations}\n".encode(), b"")
    if "FROM chunk_embedding" in query:
        return bk.CommandResult(0, f"{stub.embedding_rows}\n".encode(), b"")
    if "FROM chunk" in query:
        return bk.CommandResult(0, f"{stub.chunk_rows}\n".encode(), b"")
    if "FROM document d JOIN document_version" in query:
        return bk.CommandResult(0, f"{stub.active_mismatches}\n".encode(), b"")
    if "FROM ingest_job j" in query:
        return bk.CommandResult(0, f"{stub.jobs_missing_generation}\n".encode(), b"")
    return bk.CommandResult(0, b"\n", b"")


# ---------------------------------------------------------------------------
# 快照与 tar 构造辅助
# ---------------------------------------------------------------------------


def blob_ref(content: bytes, kb_id: str | None = None) -> tuple[str, str]:
    kb = kb_id or str(uuid.uuid4())
    return f"{kb}/{hashlib.sha256(content).hexdigest()}", hashlib.sha256(content).hexdigest()


def make_export_tar(blobs: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for ref, content in blobs.items():
            info = tarfile.TarInfo(name=f"./{ref}")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
    return buffer.getvalue()


def write_snapshot(
    root: Path,
    *,
    dump: bytes = b"\x00\x01DUMP\xff",
    blobs: dict[str, bytes] | None = None,
    source_project: str = "citemind",
    source_database: str = "citemind",
    revision: str = "20260929_0016",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    blob_map = blobs if blobs is not None else {}
    (root / bk.DATABASE_DUMP_FILENAME).write_bytes(dump)
    documents = root / bk.DOCUMENTS_DIRNAME
    entries: list[bk.BlobEntry] = []
    for ref, content in blob_map.items():
        path = documents / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        entries.append(bk.BlobEntry(file_ref=ref, sha256=digest, size=len(content)))
    manifest = bk.SnapshotManifest(
        format_version=bk.SNAPSHOT_FORMAT_VERSION,
        created_at="2026-09-30T06:00:00Z",
        source_project=source_project,
        source_database=source_database,
        alembic_revision=revision,
        database_dump=bk.DatabaseDump(
            file=bk.DATABASE_DUMP_FILENAME, sha256=hashlib.sha256(dump).hexdigest(), size=len(dump)
        ),
        documents=tuple(entries),
        facts=bk.SnapshotFacts(
            table_count=18,
            ready_document_versions=0,
            ready_index_generations=0,
            chunk_rows=0,
            chunk_embedding_rows=0,
            document_version_file_refs=len(entries),
            active_version_mismatches=0,
            jobs_with_missing_generation=0,
        ),
    )
    (root / bk.MANIFEST_FILENAME).write_text(
        json.dumps(bk.manifest_to_dict(manifest), ensure_ascii=False), encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# 默认 dry-run 零 IO
# ---------------------------------------------------------------------------


def test_backup_dry_run_does_zero_io(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    output = tmp_path / "snap"
    code = bk.main(
        ["backup", "--project", "citemind", "--database", "citemind", "--output", str(output)],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    assert runner.calls == []
    assert not output.exists()


def test_restore_dry_run_does_zero_io(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(tmp_path / "snap"),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    assert runner.calls == []


def test_verify_dry_run_does_zero_io(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    snapshot = write_snapshot(tmp_path / "snap")
    code = bk.main(["verify", "--snapshot", str(snapshot)], runner=runner)
    assert code == bk.EXIT_OK
    assert runner.calls == []


def test_dry_run_ignores_execute_runner_and_needs_no_files(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(
        ["verify", "--snapshot", str(tmp_path / "missing")],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    assert runner.calls == []


# ---------------------------------------------------------------------------
# 名称与目标守卫
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["test", "mytest", "test_db", "citemind", "citemind_test2", "citemind_test_2", "Test"],
)
def test_loose_test_names_are_rejected(name: str) -> None:
    assert bk.is_test_database_name(name) is False
    with pytest.raises(bk.GuardError):
        bk.validate_test_database(name)


def test_strict_test_name_is_accepted() -> None:
    assert bk.is_test_database_name("citemind_test") is True
    assert bk.is_test_database_name("restore_test") is True


def test_backup_allows_isolated_source_database(tmp_path: Path) -> None:
    content = b"isolated"
    ref, _ = blob_ref(content)
    stub = DbStub(file_refs=(ref,))
    runner = FakeRunner(make_responder(stub, export_tar=make_export_tar({ref: content})))
    output = tmp_path / "snap"
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind_test",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
            "--quiesced",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    assert bk.read_manifest(output).source_database == "citemind_test"


def test_restore_rejects_same_project(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(tmp_path / "snap"),
            "--source-project",
            "same",
            "--target-project",
            "same",
            "--target-database",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert runner.calls == []


def test_backup_rejects_existing_output_without_calling_runner(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    output = tmp_path / "snap"
    output.mkdir()
    (output / "keep.txt").write_text("existing", encoding="utf-8")
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
            "--quiesced",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert runner.calls == []
    assert (output / "keep.txt").read_text(encoding="utf-8") == "existing"


def test_backup_requires_confirm_and_quiesced(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub()))
    output = tmp_path / "snap"
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            "wrong",
            "--quiesced",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert not output.exists()


# ---------------------------------------------------------------------------
# 备份成功/失败与关联核对
# ---------------------------------------------------------------------------


def test_backup_success_creates_verifiable_snapshot(tmp_path: Path) -> None:
    content = b"hello blob\n"
    ref, _digest = blob_ref(content)
    stub = DbStub(file_refs=(ref,), ready_versions=1, ready_generations=1)
    runner = FakeRunner(
        make_responder(stub, export_tar=make_export_tar({ref: content}))
    )
    output = tmp_path / "snap"
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
            "--quiesced",
        ],
        runner=runner,
        now=lambda: FIXED_NOW,
    )
    assert code == bk.EXIT_OK
    assert (output / bk.DATABASE_DUMP_FILENAME).read_bytes() == b"\x00\x01DUMP\xff"
    assert (output / bk.DOCUMENTS_DIRNAME / ref).read_bytes() == content
    manifest = bk.read_manifest(output)
    assert manifest.created_at == "2026-09-30T06:00:00Z"
    assert manifest.source_database == "citemind"
    assert bk.verify_snapshot(output).ok is True
    # 输出是原子改名，父目录不留临时目录。
    assert not list(tmp_path.glob(".citemind-backup-*.tmp"))


def test_backup_cross_check_rejects_missing_blob_and_cleans_temp(tmp_path: Path) -> None:
    ref, _ = blob_ref(b"missing")
    stub = DbStub(file_refs=(ref,))
    runner = FakeRunner(make_responder(stub, export_tar=make_export_tar({})))
    output = tmp_path / "snap"
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
            "--quiesced",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_IO
    assert not output.exists()
    assert not list(tmp_path.glob(".citemind-backup-*.tmp"))


def test_backup_rejects_inconsistent_active_version(tmp_path: Path) -> None:
    runner = FakeRunner(make_responder(DbStub(active_mismatches=1)))
    output = tmp_path / "snap"
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
            "--quiesced",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_IO
    assert not output.exists()


def test_backup_does_not_overwrite_after_success(tmp_path: Path) -> None:
    content = b"x"
    ref, _ = blob_ref(content)
    stub = DbStub(file_refs=(ref,))
    runner = FakeRunner(make_responder(stub, export_tar=make_export_tar({ref: content})))
    output = tmp_path / "snap"
    args = [
        "backup",
        "--project",
        "citemind",
        "--database",
        "citemind",
        "--output",
        str(output),
        "--execute",
        "--confirm",
        str(output),
        "--quiesced",
    ]
    assert bk.main(args, runner=runner) == bk.EXIT_OK
    assert bk.main(args, runner=runner) == bk.EXIT_GUARD


# ---------------------------------------------------------------------------
# 恢复守卫与失败清理
# ---------------------------------------------------------------------------


def test_restore_allows_same_database_name_in_other_project(tmp_path: Path) -> None:
    content = b"drill"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(
        tmp_path / "snap",
        blobs={ref: content},
        source_project="src",
        source_database="citemind_test",
    )
    runner = FakeRunner(make_responder(DbStub(file_refs=(ref,), ready_versions=0)))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    assert any("pg_restore" in call for call in runner.calls)


def test_restore_rejects_manifest_project_mismatch(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap", source_project="other")
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "declared",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert runner.calls == []


def test_restore_rejects_missing_target_database(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap", source_project="src")
    runner = FakeRunner(make_responder(DbStub(database_exists=False)))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert not any("pg_restore" in call for call in runner.calls)


def test_restore_rejects_nonempty_target_database(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap", source_project="src")
    runner = FakeRunner(make_responder(DbStub(target_table_count=5)))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert not any("pg_restore" in call for call in runner.calls)


def test_restore_rejects_nonempty_blob_volume(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap", source_project="src")
    runner = FakeRunner(make_responder(DbStub(), volume_empty=False))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_GUARD
    assert not any("pg_restore" in call for call in runner.calls)


def test_restore_success_and_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"payload"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    temp_root = tmp_path / "systemtmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    ok_runner = FakeRunner(make_responder(DbStub(file_refs=(ref,), ready_versions=1)))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=ok_runner,
    )
    assert code == bk.EXIT_OK
    assert any("pg_restore" in call for call in ok_runner.calls)
    assert list(temp_root.iterdir()) == []

    fail_runner = FakeRunner(make_responder(DbStub(), fail_pg_restore=True))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=fail_runner,
    )
    assert code == bk.EXIT_COMMAND
    assert list(temp_root.iterdir()) == []


def test_restore_rejects_corrupt_snapshot_before_any_command(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap", source_project="src")
    (snapshot / bk.DATABASE_DUMP_FILENAME).write_bytes(b"tampered")
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_IO
    assert runner.calls == []


# ---------------------------------------------------------------------------
# 清单校验与篡改拒绝
# ---------------------------------------------------------------------------


def test_verify_execute_offline_passes(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content})
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(["verify", "--snapshot", str(snapshot), "--execute"], runner=runner)
    assert code == bk.EXIT_OK
    assert runner.calls == []


def test_verify_execute_fails_on_tampered_snapshot(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap")
    (snapshot / bk.DATABASE_DUMP_FILENAME).write_bytes(b"tampered")
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(["verify", "--snapshot", str(snapshot), "--execute"], runner=runner)
    assert code == bk.EXIT_VERIFY_FAILED
    assert runner.calls == []


def test_verify_execute_with_target_compares_facts(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    matching = DbStub(file_refs=(ref,), ready_versions=0, ready_generations=0)
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=FakeRunner(
            make_responder(matching, export_tar=make_export_tar({ref: content}))
        ),
    )
    assert code == bk.EXIT_OK

    mismatched = DbStub(revision="20260929_0015")
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=FakeRunner(
            make_responder(mismatched, export_tar=make_export_tar({ref: content}))
        ),
    )
    assert code == bk.EXIT_VERIFY_FAILED


def test_verify_expected_head_checks_manifest_without_target(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap", revision="20260929_0016")
    runner = FakeRunner(make_responder(DbStub()))
    code = bk.main(
        ["verify", "--snapshot", str(snapshot), "--expected-head", "20260929_0016", "--execute"],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    assert runner.calls == []

    code = bk.main(
        ["verify", "--snapshot", str(snapshot), "--expected-head", "20260929_0015", "--execute"],
        runner=runner,
    )
    assert code == bk.EXIT_VERIFY_FAILED
    assert runner.calls == []


def test_verify_target_volume_requires_existing_volume(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    stub = DbStub(file_refs=(ref,))
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=FakeRunner(make_responder(stub, volume_exists=False)),
    )
    assert code == bk.EXIT_VERIFY_FAILED


def test_verify_target_volume_missing_db_referenced_blob(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    stub = DbStub(file_refs=(ref,))
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=FakeRunner(make_responder(stub, export_tar=make_export_tar({}))),
    )
    assert code == bk.EXIT_VERIFY_FAILED


def test_verify_target_volume_rejects_tampered_blob(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    stub = DbStub(file_refs=(ref,))
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=FakeRunner(
            make_responder(stub, export_tar=make_export_tar({ref: b"tampered"}))
        ),
    )
    assert code == bk.EXIT_VERIFY_FAILED


def test_verify_target_volume_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    stub = DbStub(file_refs=(ref,))
    temp_root = tmp_path / "systemtmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=FakeRunner(
            make_responder(stub, export_tar=make_export_tar({ref: content}))
        ),
    )
    assert code == bk.EXIT_OK
    assert list(temp_root.iterdir()) == []


def test_read_manifest_rejects_symlink_by_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = write_snapshot(tmp_path / "snap")
    target = snapshot / bk.MANIFEST_FILENAME
    real_lstat = os.lstat

    def fake_lstat(path: Path) -> object:
        if Path(path) == target:
            return _FakeStat(stat_module.S_IFLNK | 0o777)
        return real_lstat(path)

    monkeypatch.setattr(bk, "_lstat", fake_lstat)
    with pytest.raises(bk.BackupError):
        bk.read_manifest(snapshot)


def test_verify_snapshot_rejects_non_regular_dump_by_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = write_snapshot(tmp_path / "snap")
    target = snapshot / bk.DATABASE_DUMP_FILENAME
    real_lstat = os.lstat

    def fake_lstat(path: Path) -> object:
        if Path(path) == target:
            return _FakeStat(stat_module.S_IFIFO | 0o644)
        try:
            return real_lstat(path)
        except OSError:
            return None

    monkeypatch.setattr(bk, "_lstat", fake_lstat)
    verification = bk.verify_snapshot(snapshot)
    assert verification.ok is False
    assert any("database.dump" in problem for problem in verification.problems)


def test_verify_snapshot_rejects_symlink_blob_by_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content})
    target = snapshot / bk.DOCUMENTS_DIRNAME / ref
    real_lstat = os.lstat

    def fake_lstat(path: Path) -> object:
        if Path(path) == target:
            return _FakeStat(stat_module.S_IFLNK | 0o777)
        return real_lstat(path)

    monkeypatch.setattr(bk, "_lstat", fake_lstat)
    verification = bk.verify_snapshot(snapshot)
    assert verification.ok is False
    assert any("符号链接或非普通文件" in problem for problem in verification.problems)


def test_build_restore_tar_rejects_symlink_by_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content})
    target = snapshot / bk.DOCUMENTS_DIRNAME / ref
    real_lstat = os.lstat

    def fake_lstat(path: Path) -> object:
        if Path(path) == target:
            return _FakeStat(stat_module.S_IFLNK | 0o777)
        return real_lstat(path)

    monkeypatch.setattr(bk, "_lstat", fake_lstat)
    with pytest.raises(bk.BackupError):
        bk.build_restore_tar(snapshot / bk.DOCUMENTS_DIRNAME, tmp_path / "restore.tar")


@pytest.mark.skipif(os.name == "nt", reason="Windows 创建符号链接需要特权；Linux 上验证真实路径")
def test_verify_snapshot_rejects_real_symlink_on_linux(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content})
    target = snapshot / bk.DOCUMENTS_DIRNAME / ref
    target.unlink()
    target.symlink_to(snapshot / bk.DATABASE_DUMP_FILENAME)
    verification = bk.verify_snapshot(snapshot)
    assert verification.ok is False
    assert any("符号链接或非普通文件" in problem for problem in verification.problems)


def test_verify_snapshot_accepts_intact(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content})
    verification = bk.verify_snapshot(snapshot)
    assert verification.ok is True
    assert verification.blob_count == 1
    assert verification.blob_bytes == 3


def test_verify_snapshot_detects_tampered_dump(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap")
    (snapshot / bk.DATABASE_DUMP_FILENAME).write_bytes(b"other")
    problems = bk.verify_snapshot(snapshot).problems
    assert any("database.dump 摘要不匹配" in problem for problem in problems)
    assert any("database.dump 大小不匹配" in problem for problem in problems)


def test_verify_snapshot_detects_tampered_blob(tmp_path: Path) -> None:
    ref, _ = blob_ref(b"abc")
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: b"abc"})
    (snapshot / bk.DOCUMENTS_DIRNAME / ref).write_bytes(b"wxyz")
    problems = bk.verify_snapshot(snapshot).problems
    assert any("blob 摘要不匹配" in problem for problem in problems)
    assert any("blob 大小不匹配" in problem for problem in problems)


def test_verify_snapshot_detects_extra_and_missing_blobs(tmp_path: Path) -> None:
    ref, _ = blob_ref(b"abc")
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: b"abc"})
    extra_dir = snapshot / bk.DOCUMENTS_DIRNAME / str(uuid.uuid4())
    extra_dir.mkdir(parents=True)
    (extra_dir / ("0" * 64)).write_bytes(b"x")
    problems = bk.verify_snapshot(snapshot).problems
    assert any("清单外" in problem for problem in problems)

    (snapshot / bk.DOCUMENTS_DIRNAME / ref).unlink()
    problems = bk.verify_snapshot(snapshot).problems
    assert any("缺少 blob" in problem for problem in problems)


def test_manifest_parse_rejects_unknown_version(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap")
    manifest_path = snapshot / bk.MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["formatVersion"] = "other"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(bk.BackupError):
        bk.read_manifest(snapshot)


def test_manifest_parse_rejects_bad_internal_consistency(tmp_path: Path) -> None:
    snapshot = write_snapshot(tmp_path / "snap")
    manifest_path = snapshot / bk.MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["facts"]["documentVersionFileRefs"] = -1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(bk.BackupError):
        bk.read_manifest(snapshot)


# ---------------------------------------------------------------------------
# schema/文件关联事实比较
# ---------------------------------------------------------------------------


def test_compare_facts_reports_mismatches(tmp_path: Path) -> None:
    ref, _ = blob_ref(b"abc")
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: b"abc"})
    manifest = bk.read_manifest(snapshot)
    missing_ref, _ = blob_ref(b"not-in-snapshot")
    facts = bk.DatabaseFacts(
        alembic_revision="20260929_0015",
        table_count=18,
        file_refs=(missing_ref,),
        ready_document_versions=0,
        ready_index_generations=0,
        chunk_rows=0,
        chunk_embedding_rows=0,
        active_version_mismatches=2,
        jobs_with_missing_generation=1,
    )
    problems = bk.compare_facts(manifest, facts)
    assert any("alembic revision 不一致" in problem for problem in problems)
    assert any("active_version_id" in problem for problem in problems)
    assert any("缺失 index_generation" in problem for problem in problems)
    assert any("快照缺失的 blob" in problem for problem in problems)


def test_compare_facts_accepts_matching() -> None:
    manifest = bk.SnapshotManifest(
        format_version=bk.SNAPSHOT_FORMAT_VERSION,
        created_at="2026-09-30T06:00:00Z",
        source_project="citemind",
        source_database="citemind",
        alembic_revision="20260929_0016",
        database_dump=bk.DatabaseDump(
            file=bk.DATABASE_DUMP_FILENAME, sha256="0" * 64, size=1
        ),
        documents=(),
        facts=bk.SnapshotFacts(18, 1, 1, 5, 5, 0, 0, 0),
    )
    facts = bk.DatabaseFacts(
        alembic_revision="20260929_0016",
        table_count=18,
        file_refs=(),
        ready_document_versions=1,
        ready_index_generations=1,
        chunk_rows=5,
        chunk_embedding_rows=5,
        active_version_mismatches=0,
        jobs_with_missing_generation=0,
    )
    assert bk.compare_facts(manifest, facts) == ()


# ---------------------------------------------------------------------------
# argv 固定、stderr 脱敏、二进制 stdout 与超时
# ---------------------------------------------------------------------------


def test_argv_are_fixed_and_shell_free() -> None:
    assert bk.pg_dump_argv(CONTAINER_ID, "citemind")[:2] == ["docker", "exec"]
    assert bk.pg_restore_argv(CONTAINER_ID, "tgt_test")[-3:] == [
        "--no-owner",
        "--exit-on-error",
        "--single-transaction",
    ]
    assert bk.blob_export_argv(IMAGE, "citemind_api-documents")[:4] == [
        "docker",
        "run",
        "--rm",
        "-v",
    ]
    assert bk.volume_inspect_argv("citemind_api-documents")[:3] == [
        "docker",
        "volume",
        "inspect",
    ]
    assert bk.blob_volume_name("citemind") == "citemind_api-documents"


def test_app_owner_rules_are_fixed_and_shell_free() -> None:
    assert (bk.APP_UID, bk.APP_GID) == (10001, 10001)
    export = bk.blob_export_argv(IMAGE, "tgt_api-documents", as_app_user=True)
    assert export[:4] == ["docker", "run", "--rm", "--user"]
    assert export[4] == "10001:10001"
    root_export = bk.blob_export_argv(IMAGE, "src_api-documents")
    assert "--user" not in root_export
    ownership = bk.blob_ownership_argv(IMAGE, "tgt_api-documents")
    assert ownership[:4] == ["docker", "run", "--rm", "-v"]
    assert ownership[4] == "tgt_api-documents:/dst"
    assert ownership[-3:] == ["sh", "-c", bk.BLOB_OWNERSHIP_SCRIPT]
    assert bk.BLOB_OWNERSHIP_SCRIPT == "chown -R 10001:10001 /dst && chmod -R u+rwX /dst"
    assert "tgt_api-documents" not in bk.BLOB_OWNERSHIP_SCRIPT


def test_build_restore_tar_sets_app_owner_and_directories(tmp_path: Path) -> None:
    first = b"first"
    second = b"second"
    kb_one, _ = blob_ref(first)
    kb_two, _ = blob_ref(second, kb_id=str(uuid.uuid4()))
    snapshot = write_snapshot(tmp_path / "snap", blobs={kb_one: first, kb_two: second})
    tar_path = tmp_path / "restore.tar"
    bk.build_restore_tar(snapshot / bk.DOCUMENTS_DIRNAME, tar_path)
    with tarfile.open(tar_path, "r:*") as archive:
        members = {member.name: member for member in archive.getmembers()}
    for file_ref in (kb_one, kb_two):
        member = members[file_ref]
        assert member.isfile()
        assert (member.uid, member.gid) == (bk.APP_UID, bk.APP_GID)
        assert member.mode == 0o644
        directory = members[file_ref.split("/")[0]]
        assert directory.isdir()
        assert (directory.uid, directory.gid) == (bk.APP_UID, bk.APP_GID)
        assert directory.mode == 0o755


def test_restore_runs_ownership_fixup_on_target_volume(tmp_path: Path) -> None:
    content = b"drill"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    runner = FakeRunner(make_responder(DbStub(file_refs=(ref,))))
    code = bk.main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--source-project",
            "src",
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
            "--confirm",
            "citemind_test",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    import_index = next(i for i, call in enumerate(runner.calls) if "tar" in call and "-xf" in call)
    ownership_calls = [
        call
        for call in runner.calls
        if call[0:2] == ["docker", "run"] and call[-2] == "-c"
    ]
    assert ownership_calls
    ownership_index = runner.calls.index(ownership_calls[0])
    assert ownership_index > import_index
    assert ownership_calls[0][-1] == bk.BLOB_OWNERSHIP_SCRIPT
    assert ownership_calls[0][4] == "tgt_api-documents:/dst"
    assert not any("src_api-documents" in call for call in runner.calls[import_index:])


def test_backup_export_stays_root_not_app_user(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    runner = FakeRunner(
        make_responder(DbStub(file_refs=(ref,)), export_tar=make_export_tar({ref: content}))
    )
    output = tmp_path / "snap"
    code = bk.main(
        [
            "backup",
            "--project",
            "citemind",
            "--database",
            "citemind",
            "--output",
            str(output),
            "--execute",
            "--confirm",
            str(output),
            "--quiesced",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    export_calls = [call for call in runner.calls if "tar" in call and "-cf" in call]
    assert export_calls
    assert all("--user" not in call for call in export_calls)


def test_verify_target_volume_export_uses_app_user(tmp_path: Path) -> None:
    content = b"abc"
    ref, _ = blob_ref(content)
    snapshot = write_snapshot(tmp_path / "snap", blobs={ref: content}, source_project="src")
    runner = FakeRunner(
        make_responder(DbStub(file_refs=(ref,)), export_tar=make_export_tar({ref: content}))
    )
    code = bk.main(
        [
            "verify",
            "--snapshot",
            str(snapshot),
            "--target-project",
            "tgt",
            "--target-database",
            "citemind_test",
            "--execute",
        ],
        runner=runner,
    )
    assert code == bk.EXIT_OK
    export_calls = [call for call in runner.calls if "tar" in call and "-cf" in call]
    assert export_calls
    assert export_calls[0][:5] == ["docker", "run", "--rm", "--user", "10001:10001"]


def test_sanitize_stderr_redacts_secrets() -> None:
    raw = (
        b"connection failed: postgresql://user:secret@host/db "
        b"password=abc Authorization: Bearer token"
    )
    text = bk.sanitize_stderr(raw)
    assert "secret" not in text
    assert "password=abc" not in text
    assert "postgresql://" not in text


def test_subprocess_runner_preserves_binary_stdout(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    script = (
        "import sys; "
        "sys.stdout.buffer.write(bytes([255, 254, 0, 128])); "
        "sys.stdout.buffer.flush()"
    )
    bk.SubprocessRunner().run(
        [sys.executable, "-c", script], stdout_path=target, timeout_seconds=60
    )
    assert target.read_bytes() == bytes([255, 254, 0, 128])


def test_subprocess_runner_timeout_is_bounded() -> None:
    started = time.monotonic()
    with pytest.raises(bk.CommandTimeout):
        bk.SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.5,
        )
    assert time.monotonic() - started < 20


def test_snapshot_version_is_pinned() -> None:
    assert bk.SNAPSHOT_FORMAT_VERSION == "citemind-backup-v1"


def test_phase4_restore_overlay_switches_only_api_and_worker_database_urls() -> None:
    """phase4-restore.yml 只覆盖 api/worker 的 DATABASE_URL 到 _test，角色/密码表达式不变。"""

    repo_root = Path(__file__).parents[2]
    compose_dir = repo_root / "deploy" / "compose"
    overlay = (compose_dir / "phase4-restore.yml").read_text("utf-8")
    base = (compose_dir / "compose.yml").read_text("utf-8")

    base_urls = [
        line.split("DATABASE_URL:", 1)[1].strip().strip('"')
        for line in base.splitlines()
        if line.strip().startswith("DATABASE_URL:")
    ]
    assert len(base_urls) == 2
    api_url, worker_url = (
        url[: -len("/citemind")] + "/citemind_test" for url in base_urls
    )

    # overlay 只应有 7 条实际缩进行，且 DATABASE_URL 完全等于 base 原标量只换库名。
    actual = [
        raw.rstrip()
        for raw in overlay.splitlines()
        if raw.strip() and not raw.lstrip().startswith("#")
    ]
    assert actual == [
        "services:",
        "  api:",
        "    environment:",
        f"      DATABASE_URL: {api_url}",
        "  worker:",
        "    environment:",
        f"      DATABASE_URL: {worker_url}",
    ]

    # driver、角色与必填 PASSWORD 表达式必须与 base 一致，不得换成别的角色或明文密码。
    assert api_url.startswith("postgresql+psycopg://citemind_api:")
    assert worker_url.startswith("postgresql+psycopg://citemind_worker:")
    assert "${API_DB_PASSWORD:?必须设置 API_DB_PASSWORD}" in api_url
    assert "${WORKER_DB_PASSWORD:?必须设置 WORKER_DB_PASSWORD}" in worker_url
