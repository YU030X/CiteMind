"""数据库与原文件的最小备份、隔离恢复与只读校验入口。

这是显式手动调用的运维命令，不是业务路径：不注册 HTTP 路由、不随 api/worker 启动、
不自动运行。本模块只使用 Python 标准库，并复用既有 Docker Compose 里运行中的
`pgvector/pgvector:pg17` 容器内的 `pg_dump`/`pg_restore`/`psql`，不依赖宿主 `pg` 客户端，
也不新增依赖或第二套实现。

安全与边界（与部署设计和开发约定一致）：

- 默认 dry-run：不传 `--execute` 时只做纯参数校验并打印计划，不连接任何服务、不读写
  文件、不构造任何客户端。真实操作必须显式 `--execute`，写入类操作还要 `--confirm`
  与写入对象完全一致；备份额外要求操作者用 `--quiesced` 声明已暂停 API/worker 写入。
- 备份数据库用 `pg_dump --format=custom`（二进制，经 `docker exec` 走容器内本地 socket，
  不传 DSN/password），原文件只复制内容寻址的 `api-documents` 卷；不含模型、缓存、
  `.env` 或任何密钥。所有子进程都是固定 argv、`shell=False`，标识符经过白名单校验。
- 数据库与文件是**顺序分拷贝，不是原子快照**：要求操作者先暂停写入再声明 `--quiesced`，
  拷贝后用 `document_version.file_ref` 与快照 blob 交叉核对，缺任何一个即整体失败。
- 输出写入同目录下自建的临时目录，成功后原子改名到目标；目标已存在时拒绝覆盖，失败时
  只清理本次自建的临时资源。
- 恢复只允许显式指定、且与源 project 不同、库名严格以 `_test` 结尾的隔离目标；拒绝
  `test`/`mytest`/`test_db` 这类宽松匹配，拒绝覆盖已含业务表的目标库或非空 blob 卷，
  不使用 `--clean`/DROP，不重置用户数据。恢复后用固定脚本把空 target 卷的卷根与目录归
  运行时用户 `10001:10001` 并给 owner 写权限，只对本次显式恢复的空 target 卷执行，不改源卷。
- 子进程有界等待，超时真实 `kill` 并回收；stdout 以二进制写入文件，不做文本编码转换；
  传给用户的 stderr 经过脱敏与截断，不回显 DSN、password 或 cookie。

未验证边界（本轮未真实执行）：真实 `pg_dump`/`pg_restore` 与卷导出仅按固定 argv 构造，
未在真实 Docker/PostgreSQL 上跑过；容器内本地 socket 认证、`pg_restore --single-transaction`
行为、跨 project 的具名卷命名与大 dump/大卷的流式内存都仍需真实验收。恢复校验只证明
schema 与引用级一致性，不证明全表字节完全同一、向量数值等价、pgvector ANN 索引重建或
WAL/跨文件原子性。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

# ---------------------------------------------------------------------------
# 常量与退出码
# ---------------------------------------------------------------------------

SNAPSHOT_FORMAT_VERSION = "citemind-backup-v1"
MANIFEST_FILENAME = "manifest.json"
DATABASE_DUMP_FILENAME = "database.dump"
DOCUMENTS_DIRNAME = "documents"
BLOB_HELPER_SOURCE = "/src"
BLOB_HELPER_DESTINATION = "/dst"
# api/worker 运行时用户：deploy/compose/Dockerfile 的 runtime stage 以 uid/gid 10001 创建并
# `USER citemind` 运行；恢复后的原文件卷必须归它所有且可写，否则 API 无法写入新 blob。
APP_UID = 10001
APP_GID = 10001
BLOB_OWNERSHIP_SCRIPT = (
    f"chown -R {APP_UID}:{APP_GID} {BLOB_HELPER_DESTINATION} "
    f"&& chmod -R u+rwX {BLOB_HELPER_DESTINATION}"
)

PGVECTOR_SERVICE = "postgres"
BLOB_VOLUME_KEY = "api-documents"
SOURCE_DATABASE_ROLE = "citemind_migrator"
TEST_DATABASE_SUFFIX = "_test"

DEFAULT_TIMEOUT_SECONDS = 1800.0
_STDERR_LIMIT = 2000
_READ_CHUNK_BYTES = 1024 * 1024

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GUARD = 3
EXIT_IO = 4
EXIT_COMMAND = 5
EXIT_VERIFY_FAILED = 6

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_DATABASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

# 容器内环境白名单：剥离业务 DSN 与密钥，避免 docker/pg 工具意外读取。
_SAFE_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CONFIG",
)

# 固定、只读、参数无用户输入的检查 SQL。
_SQL_ALEMBIC = "SELECT version_num FROM alembic_version;"
_SQL_TABLE_COUNT = (
    "SELECT count(*) FROM information_schema.tables "
    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema');"
)
_SQL_FILE_REFS = (
    "SELECT file_ref FROM document_version WHERE file_ref IS NOT NULL ORDER BY file_ref;"
)
_SQL_READY_VERSIONS = "SELECT count(*) FROM document_version WHERE status = 'READY';"
_SQL_READY_GENERATIONS = "SELECT count(*) FROM index_generation WHERE status = 'READY';"
_SQL_CHUNK_ROWS = "SELECT count(*) FROM chunk;"
_SQL_EMBEDDING_ROWS = "SELECT count(*) FROM chunk_embedding;"
_SQL_ACTIVE_VERSION_MISMATCH = (
    "SELECT count(*) FROM document d JOIN document_version v ON v.id = d.active_version_id "
    "WHERE v.document_id <> d.id OR v.status <> 'READY';"
)
_SQL_JOBS_MISSING_GENERATION = (
    "SELECT count(*) FROM ingest_job j LEFT JOIN index_generation g ON g.id = j.generation_id "
    "WHERE j.generation_id IS NOT NULL AND g.id IS NULL;"
)
_SQL_DATABASE_EXISTS = "SELECT 1 FROM pg_database WHERE datname = '{database}';"

CONSISTENCY_METHOD = "operator-declared-quiesced"
CONSISTENCY_NOTE = (
    "数据库与原文件是顺序分拷贝，不是原子快照；仅在操作者暂停 API/worker 写入并在执行时"
    "声明 --quiesced 时才成立。恢复校验只证明 schema 与引用级一致性，不证明全表字节"
    "完全同一、向量数值等价、pgvector ANN 索引重建或 WAL/跨文件原子性。"
)


class BackupError(RuntimeError):
    """备份/恢复/校验的领域失败；信息静态、可定位。"""


class GuardError(BackupError):
    """参数越过安全守卫（非测试目标、同源、非空目标等）。"""


class CommandError(BackupError):
    """外部命令以非零状态结束；stderr 已脱敏且有界。"""

    def __init__(self, message: str, *, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


class CommandTimeout(BackupError):
    """外部命令超过有界时限，已被终止并回收。"""


# ---------------------------------------------------------------------------
# 外部命令执行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        input_path: Path | None = None,
        stdout_path: Path | None = None,
        timeout_seconds: float,
        check: bool = True,
    ) -> CommandResult: ...


def sanitized_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """按白名单构造子进程环境，剥离业务 DSN 与密钥。"""

    source = environment if environment is not None else dict(os.environ)
    return {key: source[key] for key in _SAFE_ENV_KEYS if key in source}


def sanitize_stderr(raw: bytes) -> str:
    """把子进程 stderr 脱敏、截断成可安全打印的单行诊断。"""

    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"postgres(?:ql)?(?:\+psycopg)?://[^\s\"']+", "<dsn>", text)
    text = re.sub(r"(?i)password\s*=\s*\S+", "password=<redacted>", text)
    text = re.sub(r"(?i)(authorization|bearer)\s*[:=]\s*\S+", r"\1 <redacted>", text)
    text = " ".join(text.split())
    return text[:_STDERR_LIMIT]


class SubprocessRunner:
    """真实子进程执行器：固定 argv、`shell=False`、有界等待、二进制 stdin/stdout。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        input_path: Path | None = None,
        stdout_path: Path | None = None,
        timeout_seconds: float,
        check: bool = True,
    ) -> CommandResult:
        if input_bytes is not None and input_path is not None:
            raise ValueError("不能同时提供 input_bytes 与 input_path")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("超时必须是正数")

        stdout_handle = None if stdout_path is None else stdout_path.open("wb")
        stdin_handle = None if input_path is None else input_path.open("rb")
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=stdin_handle if stdin_handle is not None else (
                    subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL
                ),
                stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                env=sanitized_environment(),
            )
            try:
                stdout, stderr = process.communicate(
                    input=input_bytes, timeout=float(timeout_seconds)
                )
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.communicate(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                raise CommandTimeout("外部命令超时，已终止并回收") from None
        finally:
            if stdin_handle is not None:
                stdin_handle.close()
            if stdout_handle is not None:
                stdout_handle.close()

        if process is None:  # pragma: no cover - Popen 失败时已抛出，不会到达
            raise BackupError("无法启动外部命令")
        result = CommandResult(
            returncode=process.returncode,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )
        if check and result.returncode != 0:
            raise CommandError(
                f"命令以非零状态退出({result.returncode}): {sanitize_stderr(result.stderr)}",
                returncode=result.returncode,
            )
        return result


# ---------------------------------------------------------------------------
# 命令行构造（纯函数，便于断言 argv 固定）
# ---------------------------------------------------------------------------


def postgres_container_argv(project: str) -> list[str]:
    return [
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={PGVECTOR_SERVICE}",
        "--format",
        "{{.ID}}",
    ]


def inspect_image_argv(container_id: str) -> list[str]:
    return ["docker", "inspect", "--format", "{{.Config.Image}}", container_id]


def pg_dump_argv(container_id: str, database: str) -> list[str]:
    return [
        "docker",
        "exec",
        "-i",
        container_id,
        "pg_dump",
        f"--username={SOURCE_DATABASE_ROLE}",
        f"--dbname={database}",
        "--format=custom",
        "--no-owner",
    ]


def pg_restore_argv(container_id: str, database: str) -> list[str]:
    return [
        "docker",
        "exec",
        "-i",
        container_id,
        "pg_restore",
        f"--username={SOURCE_DATABASE_ROLE}",
        f"--dbname={database}",
        "--no-owner",
        "--exit-on-error",
        "--single-transaction",
    ]


def psql_argv(container_id: str, database: str, query: str) -> list[str]:
    return [
        "docker",
        "exec",
        "-i",
        container_id,
        "psql",
        f"--username={SOURCE_DATABASE_ROLE}",
        f"--dbname={database}",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        f"--command={query}",
    ]


def blob_export_argv(image: str, volume: str, *, as_app_user: bool = False) -> list[str]:
    argv = ["docker", "run", "--rm"]
    if as_app_user:
        # verify 用应用用户只读导出，能读到就自然证明恢复后的 blob 对该用户可读。
        argv += ["--user", f"{APP_UID}:{APP_GID}"]
    argv += [
        "-v",
        f"{volume}:{BLOB_HELPER_SOURCE}:ro",
        image,
        "tar",
        "-C",
        BLOB_HELPER_SOURCE,
        "-cf",
        "-",
        ".",
    ]
    return argv


def blob_import_argv(image: str, volume: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "-v",
        f"{volume}:{BLOB_HELPER_DESTINATION}",
        image,
        "tar",
        "-C",
        BLOB_HELPER_DESTINATION,
        "-xf",
        "-",
    ]


def blob_ownership_argv(image: str, volume: str) -> list[str]:
    """恢复后固定 argv：把空 target 卷整体归应用用户并给 owner 写权限。

    脚本是常量，不含任何用户参数；只对显式恢复的空 target 卷调用，绝不用于源卷。root
    tar 解包先落地，再以固定脚本 chown/chmod，不依赖宿主 stat 的 UID/GID。
    """

    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:{BLOB_HELPER_DESTINATION}",
        image,
        "sh",
        "-c",
        BLOB_OWNERSHIP_SCRIPT,
    ]


def volume_listing_argv(image: str, volume: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:{BLOB_HELPER_SOURCE}:ro",
        image,
        "find",
        BLOB_HELPER_SOURCE,
        "-mindepth",
        "1",
        "-print",
        "-quit",
    ]


def volume_inspect_argv(volume: str) -> list[str]:
    return ["docker", "volume", "inspect", volume]


# ---------------------------------------------------------------------------
# 命名与守卫（纯逻辑）
# ---------------------------------------------------------------------------


def validate_project(value: str, *, what: str) -> str:
    if not _PROJECT_RE.match(value):
        raise GuardError(f"{what} 不是合法的 Compose project 名: {value!r}")
    return value


def validate_database(value: str, *, what: str) -> str:
    if not _DATABASE_RE.match(value):
        raise GuardError(f"{what} 不是合法的 PostgreSQL 库名: {value!r}")
    return value


def is_test_database_name(value: str) -> bool:
    """严格判定隔离测试库名：合法库名且恰好以 `_test` 结尾，不做子串匹配。"""

    if not _DATABASE_RE.match(value) or not value.endswith(TEST_DATABASE_SUFFIX):
        return False
    return value != TEST_DATABASE_SUFFIX


def validate_test_database(value: str) -> str:
    if not is_test_database_name(value):
        raise GuardError(
            f"恢复目标库必须是严格以 {TEST_DATABASE_SUFFIX!r} 结尾的隔离库名，拒绝 {value!r}"
        )
    return value


def blob_volume_name(project: str) -> str:
    return f"{project}_{BLOB_VOLUME_KEY}"


# ---------------------------------------------------------------------------
# 快照 manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobEntry:
    file_ref: str
    sha256: str
    size: int


@dataclass(frozen=True)
class DatabaseDump:
    file: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SnapshotFacts:
    table_count: int
    ready_document_versions: int
    ready_index_generations: int
    chunk_rows: int
    chunk_embedding_rows: int
    document_version_file_refs: int
    active_version_mismatches: int
    jobs_with_missing_generation: int


@dataclass(frozen=True)
class SnapshotManifest:
    format_version: str
    created_at: str
    source_project: str
    source_database: str
    alembic_revision: str
    database_dump: DatabaseDump
    documents: tuple[BlobEntry, ...]
    facts: SnapshotFacts


def _require_mapping(value: object, *, what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BackupError(f"{what} 结构不合法")
    return value


def _require_int(value: object, *, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupError(f"{what} 必须是整数")
    return value


def _require_str(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise BackupError(f"{what} 必须是字符串")
    return value


def manifest_to_dict(manifest: SnapshotManifest) -> dict[str, object]:
    return {
        "formatVersion": manifest.format_version,
        "createdAt": manifest.created_at,
        "sourceProject": manifest.source_project,
        "sourceDatabase": manifest.source_database,
        "alembicRevision": manifest.alembic_revision,
        "databaseDump": {
            "file": manifest.database_dump.file,
            "sha256": manifest.database_dump.sha256,
            "size": manifest.database_dump.size,
        },
        "documents": [
            {"fileRef": entry.file_ref, "sha256": entry.sha256, "size": entry.size}
            for entry in manifest.documents
        ],
        "snapshot": {
            "documentBlobCount": len(manifest.documents),
            "documentBlobBytes": sum(entry.size for entry in manifest.documents),
        },
        "consistency": {
            "method": CONSISTENCY_METHOD,
            "atomicSnapshot": False,
            "note": CONSISTENCY_NOTE,
        },
        "facts": {
            "tableCount": manifest.facts.table_count,
            "readyDocumentVersions": manifest.facts.ready_document_versions,
            "readyIndexGenerations": manifest.facts.ready_index_generations,
            "chunkRows": manifest.facts.chunk_rows,
            "chunkEmbeddingRows": manifest.facts.chunk_embedding_rows,
            "documentVersionFileRefs": manifest.facts.document_version_file_refs,
            "activeVersionMismatches": manifest.facts.active_version_mismatches,
            "jobsWithMissingGeneration": manifest.facts.jobs_with_missing_generation,
        },
    }


def manifest_from_dict(payload: object) -> SnapshotManifest:
    root = _require_mapping(payload, what="manifest")
    if root.get("formatVersion") != SNAPSHOT_FORMAT_VERSION:
        raise BackupError(f"manifest formatVersion 不是 {SNAPSHOT_FORMAT_VERSION}")
    dump = _require_mapping(root.get("databaseDump"), what="databaseDump")
    raw_docs = root.get("documents")
    if not isinstance(raw_docs, list):
        raise BackupError("manifest documents 必须是列表")
    documents = tuple(_blob_entry_from_dict(item) for item in raw_docs)
    facts = _require_mapping(root.get("facts"), what="facts")
    manifest = SnapshotManifest(
        format_version=SNAPSHOT_FORMAT_VERSION,
        created_at=_require_str(root.get("createdAt"), what="createdAt"),
        source_project=_require_str(root.get("sourceProject"), what="sourceProject"),
        source_database=_require_str(root.get("sourceDatabase"), what="sourceDatabase"),
        alembic_revision=_require_str(root.get("alembicRevision"), what="alembicRevision"),
        database_dump=DatabaseDump(
            file=_require_str(dump.get("file"), what="dump.file"),
            sha256=_require_str(dump.get("sha256"), what="dump.sha256"),
            size=_require_int(dump.get("size"), what="dump.size"),
        ),
        documents=documents,
        facts=SnapshotFacts(
            table_count=_require_int(facts.get("tableCount"), what="facts.tableCount"),
            ready_document_versions=_require_int(
                facts.get("readyDocumentVersions"), what="facts.readyDocumentVersions"
            ),
            ready_index_generations=_require_int(
                facts.get("readyIndexGenerations"), what="facts.readyIndexGenerations"
            ),
            chunk_rows=_require_int(facts.get("chunkRows"), what="facts.chunkRows"),
            chunk_embedding_rows=_require_int(
                facts.get("chunkEmbeddingRows"), what="facts.chunkEmbeddingRows"
            ),
            document_version_file_refs=_require_int(
                facts.get("documentVersionFileRefs"), what="facts.documentVersionFileRefs"
            ),
            active_version_mismatches=_require_int(
                facts.get("activeVersionMismatches"), what="facts.activeVersionMismatches"
            ),
            jobs_with_missing_generation=_require_int(
                facts.get("jobsWithMissingGeneration"),
                what="facts.jobsWithMissingGeneration",
            ),
        ),
    )
    _validate_manifest_internal(manifest)
    return manifest


def _blob_entry_from_dict(item: object) -> BlobEntry:
    entry = _require_mapping(item, what="document")
    return BlobEntry(
        file_ref=_require_str(entry.get("fileRef"), what="fileRef"),
        sha256=_require_str(entry.get("sha256"), what="sha256"),
        size=_require_int(entry.get("size"), what="size"),
    )


def _validate_manifest_internal(manifest: SnapshotManifest) -> None:
    if manifest.database_dump.file != DATABASE_DUMP_FILENAME:
        raise BackupError("manifest 数据库转储文件名不是预期值")
    validate_database(manifest.source_database, what="manifest.sourceDatabase")
    validate_project(manifest.source_project, what="manifest.sourceProject")
    if not _HASH_RE.match(manifest.database_dump.sha256):
        raise BackupError("manifest 数据库转储摘要不合法")
    if manifest.database_dump.size < 0:
        raise BackupError("manifest 数据库转储大小不合法")
    seen: set[str] = set()
    for entry in manifest.documents:
        _kb_id, digest = parse_blob_ref(entry.file_ref)
        if entry.sha256 != digest:
            raise BackupError("manifest blob 摘要与 fileRef 不一致")
        if entry.size < 0:
            raise BackupError("manifest blob 大小不合法")
        if entry.file_ref in seen:
            raise BackupError("manifest 含重复 blob 引用")
        seen.add(entry.file_ref)
    if manifest.facts.document_version_file_refs < 0:
        raise BackupError("manifest 文件引用计数不合法")


def parse_blob_ref(file_ref: str) -> tuple[str, str]:
    parts = file_ref.split("/")
    if len(parts) != 2:
        raise BackupError(f"非法 blob 引用: {file_ref!r}")
    kb_part, digest = parts
    try:
        kb_id = uuid.UUID(kb_part)
    except ValueError:
        raise BackupError(f"非法 blob 引用: {file_ref!r}") from None
    if str(kb_id) != kb_part or not _HASH_RE.match(digest):
        raise BackupError(f"非法 blob 引用: {file_ref!r}")
    return kb_part, digest


# ---------------------------------------------------------------------------
# 只读校验（离线部分）
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _is_regular_file(path: Path) -> bool:
    """不跟随符号链接判定普通文件。"""

    info = _lstat(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def _is_real_directory(path: Path) -> bool:
    """不跟随符号链接判定真实目录。"""

    info = _lstat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def scan_regular_files(root: Path) -> tuple[list[str], list[str]]:
    """不跟随符号链接地枚举 ``root`` 下的普通文件；返回 (相对路径, 问题列表)。

    符号链接或非普通文件条目只记为问题，不进入结果，因此后续 SHA-256 校验绝不对符号链接
    目标生效。Windows 目录联接点可能被 ``lstat`` 报告为目录，本函数不专门检测联接点。
    """

    files: list[str] = []
    problems: list[str] = []
    if not _is_real_directory(root):
        return files, problems
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in list(dirnames):
            candidate = current / name
            if os.path.islink(candidate) or not _is_real_directory(candidate):
                problems.append(f"快照目录含符号链接或非目录条目: {candidate.name}")
                dirnames.remove(name)
        for name in filenames:
            candidate = current / name
            info = _lstat(candidate)
            if info is None or not stat.S_ISREG(info.st_mode):
                relative = candidate.relative_to(root).as_posix()
                problems.append(f"快照含符号链接或非普通文件: {relative}")
                continue
            files.append(candidate.relative_to(root).as_posix())
    return sorted(files), problems


def read_manifest(snapshot_dir: Path) -> SnapshotManifest:
    path = snapshot_dir / MANIFEST_FILENAME
    if not _is_regular_file(path):
        raise BackupError("manifest 缺失、是符号链接或不是普通文件")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise BackupError(f"无法读取 manifest: {type(error).__name__}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("manifest 不是合法 UTF-8 JSON") from None
    return manifest_from_dict(payload)


@dataclass(frozen=True)
class SnapshotVerification:
    problems: tuple[str, ...]
    blob_count: int
    blob_bytes: int
    database_bytes: int
    facts: SnapshotFacts

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_snapshot(snapshot_dir: Path) -> SnapshotVerification:
    """离线重算 SHA-256 清单并校验内容寻址不变量；不做任何网络/数据库访问。"""

    problems: list[str] = []
    manifest = read_manifest(snapshot_dir)

    dump_path = snapshot_dir / manifest.database_dump.file
    database_bytes = 0
    if not _is_regular_file(dump_path):
        problems.append("database.dump 缺失、是符号链接或不是普通文件")
    else:
        database_bytes = dump_path.stat().st_size
        if sha256_file(dump_path) != manifest.database_dump.sha256:
            problems.append("database.dump 摘要不匹配")
        if database_bytes != manifest.database_dump.size:
            problems.append("database.dump 大小不匹配")

    documents_dir = snapshot_dir / DOCUMENTS_DIRNAME
    expected = {entry.file_ref for entry in manifest.documents}
    scanned, scan_problems = scan_regular_files(documents_dir)
    problems.extend(scan_problems)
    found = set(scanned)

    blob_bytes = 0
    for entry in manifest.documents:
        if entry.file_ref not in found:
            problems.append(f"缺少 blob: {entry.file_ref}")
            continue
        blob_path = documents_dir / entry.file_ref
        size = _lstat(blob_path)
        if size is None:  # 已扫描为普通文件，仅防竞态
            problems.append(f"缺少 blob: {entry.file_ref}")
            continue
        blob_bytes += size.st_size
        if size.st_size != entry.size:
            problems.append(f"blob 大小不匹配: {entry.file_ref}")
        actual = sha256_file(blob_path)
        if actual != entry.sha256:
            problems.append(f"blob 摘要不匹配: {entry.file_ref}")
        if actual != Path(entry.file_ref).name:
            problems.append(f"blob 内容与 fileRef 摘要不一致: {entry.file_ref}")

    for relative in scanned:
        if relative not in expected:
            problems.append(f"存在清单外的 blob 文件: {relative}")

    return SnapshotVerification(
        problems=tuple(problems),
        blob_count=len(manifest.documents),
        blob_bytes=blob_bytes,
        database_bytes=database_bytes,
        facts=manifest.facts,
    )


# ---------------------------------------------------------------------------
# 数据库只读事实
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseFacts:
    alembic_revision: str
    table_count: int
    file_refs: tuple[str, ...]
    ready_document_versions: int
    ready_index_generations: int
    chunk_rows: int
    chunk_embedding_rows: int
    active_version_mismatches: int
    jobs_with_missing_generation: int


def _find_postgres_container(runner: CommandRunner, project: str, timeout: float) -> str:
    result = runner.run(postgres_container_argv(project), timeout_seconds=timeout)
    container_id = result.stdout.decode("ascii", errors="replace").strip()
    if not _CONTAINER_ID_RE.match(container_id):
        raise BackupError(
            f"未找到 project {project!r} 的运行中 postgres 容器；请先启动该 Compose 项目"
        )
    return container_id


def _container_image(runner: CommandRunner, container_id: str, timeout: float) -> str:
    result = runner.run(inspect_image_argv(container_id), timeout_seconds=timeout)
    image = result.stdout.decode("utf-8", errors="replace").strip()
    if not image or "\n" in image:
        raise BackupError("无法解析容器镜像引用")
    return image


def _query_output(
    runner: CommandRunner, container_id: str, database: str, query: str, timeout: float
) -> str:
    result = runner.run(psql_argv(container_id, database, query), timeout_seconds=timeout)
    return result.stdout.decode("utf-8", errors="replace")


def query_scalar(
    runner: CommandRunner, container_id: str, database: str, query: str, timeout: float
) -> str | None:
    output = _query_output(runner, container_id, database, query, timeout)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[0]


def query_lines(
    runner: CommandRunner, container_id: str, database: str, query: str, timeout: float
) -> tuple[str, ...]:
    output = _query_output(runner, container_id, database, query, timeout)
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _scalar_int(value: str | None) -> int:
    if value is None or not value.lstrip("-").isdigit():
        return 0
    return int(value)


def collect_database_facts(
    runner: CommandRunner, container_id: str, database: str, timeout: float
) -> DatabaseFacts:
    alembic_revision = query_scalar(
        runner, container_id, database, _SQL_ALEMBIC, timeout
    )
    if not alembic_revision:
        raise BackupError("目标库没有 alembic_version 记录；不是已迁移的 CiteMind 库")
    file_refs = query_lines(runner, container_id, database, _SQL_FILE_REFS, timeout)
    return DatabaseFacts(
        alembic_revision=alembic_revision,
        table_count=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_TABLE_COUNT, timeout)
        ),
        file_refs=file_refs,
        ready_document_versions=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_READY_VERSIONS, timeout)
        ),
        ready_index_generations=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_READY_GENERATIONS, timeout)
        ),
        chunk_rows=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_CHUNK_ROWS, timeout)
        ),
        chunk_embedding_rows=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_EMBEDDING_ROWS, timeout)
        ),
        active_version_mismatches=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_ACTIVE_VERSION_MISMATCH, timeout)
        ),
        jobs_with_missing_generation=_scalar_int(
            query_scalar(runner, container_id, database, _SQL_JOBS_MISSING_GENERATION, timeout)
        ),
    )


def database_facts_to_snapshot_facts(facts: DatabaseFacts) -> SnapshotFacts:
    return SnapshotFacts(
        table_count=facts.table_count,
        ready_document_versions=facts.ready_document_versions,
        ready_index_generations=facts.ready_index_generations,
        chunk_rows=facts.chunk_rows,
        chunk_embedding_rows=facts.chunk_embedding_rows,
        document_version_file_refs=len(facts.file_refs),
        active_version_mismatches=facts.active_version_mismatches,
        jobs_with_missing_generation=facts.jobs_with_missing_generation,
    )


# ---------------------------------------------------------------------------
# blob 卷读写
# ---------------------------------------------------------------------------


def _write_and_hash(source: IO[bytes], destination: Path) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as handle:
        while True:
            chunk = source.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def extract_exported_blobs(tar_path: Path, documents_dir: Path) -> tuple[BlobEntry, ...]:
    """从卷导出 tar 中提取内容寻址 blob，并逐文件校验文件名与 SHA-256 一致。"""

    entries: list[BlobEntry] = []
    documents_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:*") as archive:
        for member in archive.getmembers():
            name = member.name[2:] if member.name.startswith("./") else member.name
            if not name or member.isdir():
                continue
            if not member.isfile():
                raise BackupError(f"卷导出含非普通文件条目: {member.name!r}")
            _kb_id, digest = parse_blob_ref(name)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise BackupError(f"无法读取卷导出条目: {name!r}")
            size, actual = _write_and_hash(extracted, documents_dir / name)
            if actual != digest:
                raise BackupError(f"卷内 blob 内容与路径摘要不一致: {name}")
            entries.append(BlobEntry(file_ref=name, sha256=actual, size=size))
    entries.sort(key=lambda entry: entry.file_ref)
    return tuple(entries)


def export_volume_blobs(
    runner: CommandRunner,
    image: str,
    volume: str,
    documents_dir: Path,
    timeout: float,
    *,
    as_app_user: bool = False,
) -> tuple[BlobEntry, ...]:
    """把命名卷只读导出并提取为内容寻址 blob；临时 tar 只自建且 finally 清理。

    复用与备份相同的导出/校验路径：跳过 tar 目录条目，`<kb>/<sha256>` 形状与内容摘要必须一致；
    符号链接与非普通文件条目一律失败，只校验普通 blob 文件。
    """

    export_tar = documents_dir.parent / f".blobs-export-{uuid.uuid4().hex}.tar"
    try:
        runner.run(
            blob_export_argv(image, volume, as_app_user=as_app_user),
            stdout_path=export_tar,
            timeout_seconds=timeout,
        )
        return extract_exported_blobs(export_tar, documents_dir)
    finally:
        export_tar.unlink(missing_ok=True)


def build_restore_tar(documents_dir: Path, tar_path: Path) -> None:
    """从快照构建恢复用 tar：只接受普通文件，并固定条目 owner 与目录结构。

    每个条目显式写入 uid/gid ``APP_UID``/``APP_GID`` 与确定性 mode，并补上 KB 目录条目，
    使恢复结果不依赖宿主 stat 的 UID/GID（Windows 上默认 0），也不让 tar 隐式创建
    root:root 的父目录。
    """

    files, problems = scan_regular_files(documents_dir)
    if problems:
        raise BackupError("快照原文件含不安全条目: " + "; ".join(problems[:5]))
    with tarfile.open(tar_path, "w") as archive:
        directories = sorted(
            {Path(relative).parent.as_posix() for relative in files}
            - {"."}
        )
        for directory in directories:
            directory_info = tarfile.TarInfo(name=directory)
            directory_info.type = tarfile.DIRTYPE
            directory_info.mode = 0o755
            directory_info.uid = APP_UID
            directory_info.gid = APP_GID
            directory_info.uname = ""
            directory_info.gname = ""
            archive.addfile(directory_info)
        for relative in files:
            path = documents_dir / relative
            stat_info = _lstat(path)
            if stat_info is None or not stat.S_ISREG(stat_info.st_mode):
                raise BackupError(f"快照条目不是普通文件: {relative}")
            member = archive.gettarinfo(str(path), arcname=relative)
            member.uid = APP_UID
            member.gid = APP_GID
            member.uname = ""
            member.gname = ""
            member.mode = 0o644
            with path.open("rb") as handle:
                archive.addfile(member, handle)


def _volume_exists(runner: CommandRunner, volume: str, timeout: float) -> bool:
    result = runner.run(volume_inspect_argv(volume), timeout_seconds=timeout, check=False)
    return result.returncode == 0


def _volume_is_empty(
    runner: CommandRunner, image: str, volume: str, timeout: float
) -> bool:
    result = runner.run(volume_listing_argv(image, volume), timeout_seconds=timeout)
    return not result.stdout.decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------


def _new_temp_dir(output: Path) -> Path:
    temp = output.parent / f".citemind-backup-{uuid.uuid4().hex}.tmp"
    temp.mkdir(parents=True, exist_ok=False)
    return temp


def _require_output_writable(output: Path) -> None:
    if output.exists():
        raise GuardError(f"输出目录已存在，拒绝覆盖: {output}")
    if output.parent.exists() and not output.parent.is_dir():
        raise GuardError(f"输出父路径不是目录: {output.parent}")


def run_backup(
    *,
    runner: CommandRunner,
    project: str,
    database: str,
    output: Path,
    timeout: float,
    created_at: str,
    log: Callable[[str], None],
) -> SnapshotManifest:
    _require_output_writable(output)
    container_id = _find_postgres_container(runner, project, timeout)
    log(f"使用 postgres 容器 {container_id}")
    facts = collect_database_facts(runner, container_id, database, timeout)
    if facts.active_version_mismatches:
        raise BackupError("源库 document.active_version_id 一致性核对未通过，拒绝备份")
    if facts.jobs_with_missing_generation:
        raise BackupError("源库 ingest_job.generation_id 引用核对未通过，拒绝备份")

    temp = _new_temp_dir(output)
    log(f"写入临时目录 {temp}")
    try:
        dump_path = temp / DATABASE_DUMP_FILENAME
        runner.run(
            pg_dump_argv(container_id, database),
            stdout_path=dump_path,
            timeout_seconds=timeout,
        )
        dump_size = dump_path.stat().st_size
        if dump_size == 0:
            raise BackupError("pg_dump 输出为空，拒绝生成快照")
        dump_sha = sha256_file(dump_path)

        image = _container_image(runner, container_id, timeout)
        volume = blob_volume_name(project)
        documents = export_volume_blobs(
            runner, image, volume, temp / DOCUMENTS_DIRNAME, timeout
        )

        snapshot_refs = {entry.file_ref for entry in documents}
        missing = sorted(set(facts.file_refs) - snapshot_refs)
        if missing:
            raise BackupError(
                "数据库引用的原文件在快照中缺失（说明拷贝期间仍有写入，非一致快照）: "
                + ", ".join(missing[:5])
            )

        manifest = SnapshotManifest(
            format_version=SNAPSHOT_FORMAT_VERSION,
            created_at=created_at,
            source_project=project,
            source_database=database,
            alembic_revision=facts.alembic_revision,
            database_dump=DatabaseDump(
                file=DATABASE_DUMP_FILENAME, sha256=dump_sha, size=dump_size
            ),
            documents=documents,
            facts=database_facts_to_snapshot_facts(facts),
        )
        (temp / MANIFEST_FILENAME).write_text(
            json.dumps(manifest_to_dict(manifest), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, output)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    log(f"快照已生成: {output}")
    return manifest


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def _plan_restore_guards(
    *, source_project: str, target_project: str, target_database: str
) -> None:
    validate_project(source_project, what="source-project")
    validate_project(target_project, what="target-project")
    validate_test_database(target_database)
    if source_project == target_project:
        raise GuardError("恢复目标 project 与源 project 相同，拒绝在同一项目内恢复")


def run_restore(
    *,
    runner: CommandRunner,
    snapshot_dir: Path,
    source_project: str,
    target_project: str,
    target_database: str,
    timeout: float,
    log: Callable[[str], None],
) -> SnapshotManifest:
    _plan_restore_guards(
        source_project=source_project,
        target_project=target_project,
        target_database=target_database,
    )
    verification = verify_snapshot(snapshot_dir)
    if not verification.ok:
        raise BackupError("快照校验未通过，拒绝恢复: " + "; ".join(verification.problems[:5]))
    manifest = read_manifest(snapshot_dir)
    if manifest.source_project != source_project:
        raise GuardError(
            f"快照记录的源 project 是 {manifest.source_project!r}，与 --source-project "
            f"{source_project!r} 不一致"
        )
    if target_project == manifest.source_project:
        raise GuardError("恢复目标 project 与快照源 project 相同，拒绝恢复")

    container_id = _find_postgres_container(runner, target_project, timeout)
    log(f"使用目标 postgres 容器 {container_id}")
    _require_empty_target_database(runner, container_id, target_database, timeout)

    image = _container_image(runner, container_id, timeout)
    volume = blob_volume_name(target_project)
    if not _volume_exists(runner, volume, timeout):
        raise GuardError(
            f"目标原文件卷 {volume!r} 不存在；请先用隔离 Compose 项目创建空的 api-documents 卷"
        )
    if not _volume_is_empty(runner, image, volume, timeout):
        raise GuardError(f"目标原文件卷 {volume!r} 非空，拒绝覆盖")

    dump_path = snapshot_dir / manifest.database_dump.file
    log("恢复数据库（单事务）")
    runner.run(
        pg_restore_argv(container_id, target_database),
        input_path=dump_path,
        timeout_seconds=timeout,
    )

    log("恢复原文件卷")
    temp_dir = Path(tempfile.mkdtemp(prefix="citemind-restore-"))
    try:
        restore_tar = temp_dir / "documents.tar"
        build_restore_tar(snapshot_dir / DOCUMENTS_DIRNAME, restore_tar)
        runner.run(
            blob_import_argv(image, volume),
            input_path=restore_tar,
            timeout_seconds=timeout,
        )
        # 只对本次显式恢复的空 target 卷，把卷根与 KB 目录归应用用户并给 owner 写权限，
        # 否则 API（uid 10001）无法在新 KB/版本下写入 blob。
        log("修正目标卷 owner 为应用用户")
        runner.run(
            blob_ownership_argv(image, volume),
            timeout_seconds=timeout,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    log(f"恢复完成: project={target_project} database={target_database}")
    return manifest


def _require_empty_target_database(
    runner: CommandRunner, container_id: str, target_database: str, timeout: float
) -> None:
    exists = query_scalar(
        runner,
        container_id,
        "postgres",
        _SQL_DATABASE_EXISTS.format(database=target_database),
        timeout,
    )
    if exists != "1":
        raise GuardError(
            f"目标库 {target_database!r} 不存在；请用空数据卷初始化隔离项目（initdb 会建立 "
            f"{target_database!r}），本工具不创建数据库"
        )
    table_count = _scalar_int(
        query_scalar(runner, container_id, target_database, _SQL_TABLE_COUNT, timeout)
    )
    if table_count != 0:
        raise GuardError(f"目标库 {target_database!r} 已含 {table_count} 张表，拒绝覆盖")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def compare_facts(
    manifest: SnapshotManifest, facts: DatabaseFacts
) -> tuple[str, ...]:
    """比较恢复库事实与快照事实及引用一致性；返回问题列表。"""

    problems: list[str] = []
    if facts.alembic_revision != manifest.alembic_revision:
        problems.append(
            "alembic revision 不一致: "
            f"快照 {manifest.alembic_revision} / 目标 {facts.alembic_revision}"
        )
    if facts.active_version_mismatches:
        problems.append(f"document.active_version_id 不一致行数 {facts.active_version_mismatches}")
    if facts.jobs_with_missing_generation:
        problems.append(
            f"ingest_job 引用缺失 index_generation 行数 {facts.jobs_with_missing_generation}"
        )
    snapshot_refs = {entry.file_ref for entry in manifest.documents}
    database_refs = set(facts.file_refs)
    missing = sorted(database_refs - snapshot_refs)
    if missing:
        problems.append("目标库引用但快照缺失的 blob: " + ", ".join(missing[:5]))
    expected = manifest.facts
    actual = database_facts_to_snapshot_facts(facts)
    for name in (
        "ready_document_versions",
        "ready_index_generations",
        "chunk_rows",
        "chunk_embedding_rows",
        "document_version_file_refs",
    ):
        if getattr(expected, name) != getattr(actual, name):
            problems.append(
                f"{name} 不一致: 快照 {getattr(expected, name)} / 目标 {getattr(actual, name)}"
            )
    return tuple(problems)


def run_verify(
    *,
    runner: CommandRunner | None,
    snapshot_dir: Path,
    target_project: str | None,
    target_database: str | None,
    expected_head: str | None,
    timeout: float,
    log: Callable[[str], None],
) -> tuple[SnapshotVerification, tuple[str, ...]]:
    verification = verify_snapshot(snapshot_dir)
    log(
        f"离线校验: blobs={verification.blob_count} blobBytes={verification.blob_bytes} "
        f"databaseBytes={verification.database_bytes} problems={len(verification.problems)}"
    )
    for problem in verification.problems:
        log(f"  - {problem}")
    if not verification.ok:
        return verification, ()

    manifest = read_manifest(snapshot_dir)
    problems: list[str] = []
    if expected_head is not None and manifest.alembic_revision != expected_head:
        problems.append(
            "快照 alembic revision "
            f"{manifest.alembic_revision} 与 --expected-head {expected_head} 不一致"
        )
    if target_project is None or target_database is None:
        for problem in problems:
            log(f"  - {problem}")
        return verification, tuple(problems)
    if runner is None:
        raise BackupError("缺少命令执行器，无法连接目标库")

    validate_project(target_project, what="target-project")
    validate_database(target_database, what="target-database")
    container_id = _find_postgres_container(runner, target_project, timeout)
    facts = collect_database_facts(runner, container_id, target_database, timeout)
    problems.extend(compare_facts(manifest, facts))
    if expected_head is not None and facts.alembic_revision != expected_head:
        problems.append(
            f"目标 alembic revision {facts.alembic_revision} 与 "
            f"--expected-head {expected_head} 不一致"
        )
    image = _container_image(runner, container_id, timeout)
    problems.extend(
        verify_target_volume(
            runner,
            image=image,
            volume=blob_volume_name(target_project),
            facts=facts,
            manifest=manifest,
            timeout=timeout,
            log=log,
        )
    )
    log(f"目标库校验: revision={facts.alembic_revision} tables={facts.table_count}")
    for problem in problems:
        log(f"  - {problem}")
    return verification, tuple(problems)


def verify_target_volume(
    runner: CommandRunner,
    *,
    image: str,
    volume: str,
    facts: DatabaseFacts,
    manifest: SnapshotManifest,
    timeout: float,
    log: Callable[[str], None],
) -> tuple[str, ...]:
    """只读校验目标原文件卷：全部目标 blob 逐文件校验 SHA-256 并与快照/目标库一致。

    临时目录只自建且在 finally 清理；目标卷不存在、为空而目标库有引用、缺文件、内容被改
    或含非普通条目都返回问题，不默认成功。所有目标 blob 都会实际读取校验，无抽样参数。
    """

    problems: list[str] = []
    if not _volume_exists(runner, volume, timeout):
        return (f"目标原文件卷 {volume!r} 不存在",)
    temp_dir = Path(tempfile.mkdtemp(prefix="citemind-verify-"))
    try:
        try:
            entries = export_volume_blobs(
                runner, image, volume, temp_dir / DOCUMENTS_DIRNAME, timeout, as_app_user=True
            )
        except BackupError as error:
            return (f"目标原文件卷校验失败: {error}",)
        target_map = {entry.file_ref: entry.sha256 for entry in entries}
        snapshot_map = {entry.file_ref: entry.sha256 for entry in manifest.documents}
        missing = sorted(set(facts.file_refs) - set(target_map))
        if missing:
            problems.append("目标库引用但目标卷缺失的 blob: " + ", ".join(missing[:5]))
        for file_ref, sha in snapshot_map.items():
            actual = target_map.get(file_ref)
            if actual is not None and actual != sha:
                problems.append(f"目标卷 blob 摘要与快照不一致: {file_ref}")
        log(
            f"目标卷校验: volume={volume} blobs={len(entries)} "
            f"missing={len(missing)} problems={len(problems)}"
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return tuple(problems)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.operations.backup",
        description=(
            "最小数据库+原文件备份/隔离恢复入口；默认 dry-run，真实操作需 --execute 与 --confirm。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="备份数据库（pg_dump custom）与原文件卷")
    backup.add_argument("--project", required=True, help="源 Compose project 名")
    backup.add_argument("--database", required=True, help="源数据库名（业务库或隔离库均可）")
    backup.add_argument("--output", required=True, help="快照输出目录；必须不存在")
    backup.add_argument("--execute", action="store_true", help="执行真实备份（默认只打印计划）")
    backup.add_argument("--confirm", default=None, help="必须与 --output 完全一致")
    backup.add_argument(
        "--quiesced", action="store_true", help="声明已暂停 API/worker 写入（真实执行必需）"
    )

    restore = sub.add_parser("restore", help="把快照恢复到隔离的 _test 目标")
    restore.add_argument("--snapshot", required=True, help="快照目录")
    restore.add_argument("--source-project", required=True, help="快照来源 Compose project 名")
    restore.add_argument("--target-project", required=True, help="隔离目标 Compose project 名")
    restore.add_argument("--target-database", required=True, help="隔离目标库名（严格 _test 结尾）")
    restore.add_argument("--execute", action="store_true", help="执行真实恢复（默认只打印计划）")
    restore.add_argument("--confirm", default=None, help="必须与 --target-database 完全一致")

    verify = sub.add_parser(
        "verify", help="只读校验快照清单、可选目标库引用与目标原文件卷"
    )
    verify.add_argument("--snapshot", required=True, help="快照目录")
    verify.add_argument("--target-project", default=None, help="隔离目标 Compose project 名")
    verify.add_argument("--target-database", default=None, help="隔离目标库名")
    verify.add_argument(
        "--expected-head",
        default=None,
        help="期望的 alembic revision；无 --target 时核对快照记录，有 --target 时还核对目标库",
    )
    verify.add_argument("--execute", action="store_true", help="执行真实校验（默认只打印计划）")

    for sub_parser in (backup, restore, verify):
        sub_parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=DEFAULT_TIMEOUT_SECONDS,
            help=f"单个外部命令时限（默认 {DEFAULT_TIMEOUT_SECONDS:.0f} 秒）",
        )
    return parser


def _emit(message: str) -> None:
    print(message)


def _error(message: str) -> None:
    print(f"错误: {message}", file=sys.stderr)


def _validate_timeout(timeout: float) -> float:
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise GuardError("--timeout-seconds 必须是正数")
    return float(timeout)


def _main_backup(
    args: argparse.Namespace, runner: CommandRunner | None, now: Callable[[], datetime]
) -> int:
    validate_project(args.project, what="--project")
    validate_database(args.database, what="--database")
    timeout = _validate_timeout(args.timeout_seconds)
    output = Path(args.output)

    if not args.execute:
        _emit("dry-run（未执行任何 IO）")
        _emit(f"  操作: backup project={args.project} database={args.database} output={output}")
        _emit("  真实执行需要 --execute --confirm <output> --quiesced")
        return EXIT_OK

    if args.confirm != args.output:
        raise GuardError("--confirm 必须与 --output 完全一致")
    if not args.quiesced:
        raise GuardError("备份前必须暂停 API/worker 写入并用 --quiesced 显式声明")
    active_runner = runner if runner is not None else SubprocessRunner()
    manifest = run_backup(
        runner=active_runner,
        project=args.project,
        database=args.database,
        output=output,
        timeout=timeout,
        created_at=now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        log=_emit,
    )
    _emit(
        f"完成: revision={manifest.alembic_revision} blobs={len(manifest.documents)} "
        f"dumpBytes={manifest.database_dump.size}"
    )
    return EXIT_OK


def _main_restore(args: argparse.Namespace, runner: CommandRunner | None) -> int:
    _plan_restore_guards(
        source_project=args.source_project,
        target_project=args.target_project,
        target_database=args.target_database,
    )
    timeout = _validate_timeout(args.timeout_seconds)
    snapshot_dir = Path(args.snapshot)

    if not args.execute:
        _emit("dry-run（未执行任何 IO）")
        _emit(
            f"  操作: restore snapshot={snapshot_dir} target={args.target_project}/"
            f"{args.target_database}（与源 {args.source_project} 不同）"
        )
        _emit("  真实执行需要 --execute --confirm <target-database>")
        return EXIT_OK

    if args.confirm != args.target_database:
        raise GuardError("--confirm 必须与 --target-database 完全一致")
    active_runner = runner if runner is not None else SubprocessRunner()
    run_restore(
        runner=active_runner,
        snapshot_dir=snapshot_dir,
        source_project=args.source_project,
        target_project=args.target_project,
        target_database=args.target_database,
        timeout=timeout,
        log=_emit,
    )
    return EXIT_OK


def _main_verify(args: argparse.Namespace, runner: CommandRunner | None) -> int:
    timeout = _validate_timeout(args.timeout_seconds)
    snapshot_dir = Path(args.snapshot)
    if args.target_project is not None or args.target_database is not None:
        if args.target_project is None or args.target_database is None:
            raise GuardError("--target-project 与 --target-database 必须同时提供")
        validate_project(args.target_project, what="--target-project")
        validate_database(args.target_database, what="--target-database")

    if not args.execute:
        target = (
            f"{args.target_project}/{args.target_database}"
            if args.target_project is not None and args.target_database is not None
            else "未指定（仅离线清单校验）"
        )
        _emit("dry-run（未执行任何 IO）")
        _emit(f"  操作: verify snapshot={snapshot_dir} target={target}")
        _emit("  真实执行需要 --execute")
        return EXIT_OK

    verification, problems = run_verify(
        runner=runner,
        snapshot_dir=snapshot_dir,
        target_project=args.target_project,
        target_database=args.target_database,
        expected_head=args.expected_head,
        timeout=timeout,
        log=_emit,
    )
    if not verification.ok or problems:
        _error("校验发现不一致")
        return EXIT_VERIFY_FAILED
    _emit("校验通过：schema 与引用级一致；全表字节同一、向量数值等价与 ANN 重建未验证。")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    clock = now if now is not None else (lambda: datetime.now(UTC))
    try:
        if args.command == "backup":
            return _main_backup(args, runner, clock)
        if args.command == "restore":
            return _main_restore(args, runner)
        return _main_verify(args, runner)
    except CommandTimeout as error:
        _error(str(error))
        return EXIT_COMMAND
    except CommandError as error:
        _error(str(error))
        return EXIT_COMMAND
    except GuardError as error:
        _error(str(error))
        return EXIT_GUARD
    except BackupError as error:
        _error(str(error))
        return EXIT_IO
    except OSError as error:
        _error(f"IO 失败: {type(error).__name__}")
        return EXIT_IO


if __name__ == "__main__":  # pragma: no cover - 由命令行触发
    raise SystemExit(main())
