"""Demo 隔离整链验收：真实 HTTP 上传 → 真实解析/入库（假编码器）→ 真实 HTTP 问答
（假 provider）→ 引用与追问 → 版本更新旧引用标记 → 删除后隐藏与检索排除。

本文件把此前各自独立验收的上传、入库、检索、问答、更新、删除片段接到**一条**代表性串联里，
对应 ``docs/personal-demo.md`` 的个人演示流程，但用隔离破坏性测试库而不是六服务栈：

1. ``demo-hr`` 通过真实上传事务写入自制 Markdown 与文本 PDF；
2. 真实解析/切分/入库代码路径发布 ``READY`` 并产生带 ``source_locator`` 的 chunk
   （向量编码器、token 计数器、关键词分析器是显式**假实现**，不代表真实模型）；
3. 真实问答 API（生成客户端是显式**假 provider**，不联网、不代表真实模型）返回引用：
   Markdown 定位到 1-based 行区间，PDF 定位到页号，正文带可点击 ``[n]`` 内联标记；
4. 追问触发受限改写；
5. 上传新版本并发布后，历史里旧版本引用的 ``isCurrentVersion`` 变为 ``false``，
   新回答引用新版本；
6. 逻辑删除后历史助手消息消失、引用详情 404、检索不再返回该文档。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 或 Redis 时按既有契约跳过，绝不触碰开发库或
真实 ``.env``。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases, assert_destructive_matches_roles
from httpx import AsyncClient
from rag_backend.api.errors import CODE_CITATION_NOT_FOUND
from rag_backend.config import Settings
from rag_backend.database import create_sync_session_factory
from rag_backend.generation.deepseek_client import GenerationOutcome
from rag_backend.generation.query_rewrite import REWRITE_SYSTEM_PROMPT
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion.storage import DocumentBlobStore
from sqlalchemy import Engine, create_engine, text
from test_conversation_flow import (
    FakeGenerator,
    _answer_json,
    _ask,
    _context,
    _create_conversation,
    _outcome,
    qa_client,
)
from test_core_migration import alembic_config, alembic_revision, business_tables
from test_document_update_delete_flow import (
    delete_document_request,
    publish,
    upload_version,
)
from test_document_upload_flow import (
    ORGANIZATION_ID,
    api_client,
    document_row,
    job_row,
    login_csrf,
    make_settings,
    seed_kb,
    seed_member,
    seed_user,
    unique_key,
    unique_name,
    unique_username,
    upload,
)
from test_retrieval_flow import (
    FakeAnalyzer as RetrievalAnalyzer,
)
from test_retrieval_flow import (
    FakeEmbedder as RetrievalEmbedder,
)
from test_retrieval_flow import (
    api_session,
    run_search,
)

pytestmark = pytest.mark.integration

# 问答与会话表、生成选项快照都需要升到当前 head；与 ``test_conversation_flow`` 一致。
SCHEMA_REVISION = "20260927_0011"

CORPUS = Path(__file__).resolve().parents[1] / "evaluation" / "corpus"
MD_V2 = (CORPUS / "handbook-v2.md").read_bytes()
MD_V3 = (CORPUS / "handbook-v3.md").read_bytes()
PDF_TEXT = (CORPUS / "cafeteria.pdf").read_bytes()
MD_TITLE = "员工手册"
PDF_TITLE = "食堂开放时间"

# 关键词路的两路之一；向量路对所有 chunk 使用常向量，因此两篇文档都会进入融合候选。
KEYWORD_TERMS = "cafeteria"
QUERY = "食堂工作日几点开放？"
FOLLOW_UP = "那年假需要谁审批？"
STANDALONE = "正式员工每个自然年有多少天带薪年假？"

TRUNCATE_SQL = (
    "TRUNCATE citation, query_run, message, conversation, llm_usage, chunk_embedding, chunk, "
    "index_generation, outbox_event, ingest_job, document_version, document, knowledge_base, "
    "index_profile, kb_member, auth_session, user_account CASCADE"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def chain_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        owns_schema = True
        command.upgrade(config, SCHEMA_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(autouse=True)
def clean_business_rows(chain_schema: Engine) -> Iterator[None]:
    with chain_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))
    yield


@pytest.fixture(scope="module")
def blob_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("demo-chain-documents")


@pytest.fixture(scope="module")
def worker_sessions(chain_schema: Engine, role_test_databases: RoleTestDatabases) -> Iterator[Any]:
    engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        yield create_sync_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def chain_settings(
    chain_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
    test_redis: Any,
    blob_root: Path,
) -> Settings:
    assert role_test_databases.database_name == destructive_test_database.database_name
    return make_settings(
        database_url=role_test_databases.api_url,
        redis_url=test_redis.url,
        storage_directory=blob_root,
    )


class ChainGenerator(FakeGenerator):
    """在真实问答 API 里替换 provider 的显式假实现。

    回答调用引用提示里出现的**全部**证据编号，从而把本轮检索到的两篇文档都映射成引用；
    改写调用沿用 ``FakeGenerator`` 的固定独立问题。整个类不联网。
    """

    def __init__(self) -> None:
        super().__init__([_outcome(content=_answer_json())])

    def generate(
        self,
        messages: Sequence[Any],
        *,
        model: str,
        max_output_tokens: int,
        thinking: Any = None,
    ) -> GenerationOutcome:
        captured = tuple(messages)
        if captured[0].content == REWRITE_SYSTEM_PROMPT:
            return super().generate(
                messages, model=model, max_output_tokens=max_output_tokens, thinking=thinking
            )
        labels = re.findall(r"(?m)^(E\d+): ", captured[-1].content)
        if not labels:
            raise AssertionError("证据区应至少包含一条 E 编号")
        self.messages.append(captured)
        self.calls += 1
        self.answer_calls += 1
        self.answer_messages.append(captured)
        return _outcome(content=_answer_json(tuple(labels)))


@asynccontextmanager
async def demo_qa_client(
    settings: Settings,
    *,
    context: Any,
    generator: ChainGenerator,
    engine: Engine,
    kb_id: uuid.UUID,
) -> AsyncIterator[AsyncClient]:
    """问答客户端：查询编码器与分析器身份都对齐入库时登记的 profile。"""

    analyzer = RetrievalAnalyzer(
        terms=KEYWORD_TERMS, analyzer_id=kb_keyword_analyzer_version(engine, kb_id)
    )
    async with qa_client(
        settings,
        context=context,
        generator=generator,
        analyzer=analyzer,
        embedder=RetrievalEmbedder(vector=(1.0,)),
    ) as client:
        yield client


def kb_keyword_analyzer_version(engine: Engine, kb_id: uuid.UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text(
                    "SELECT p.keyword_analyzer_version FROM knowledge_base kb "
                    "JOIN index_profile p ON p.id = kb.active_index_profile_id "
                    "WHERE kb.id = :id"
                ),
                {"id": kb_id},
            )
        )


def chunk_locators(engine: Engine, document_id: uuid.UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT source_locator FROM chunk WHERE document_id = :id "
                    "ORDER BY chunk_index"
                ),
                {"id": document_id},
            ).scalars()
        ]


async def _search(
    api_url: str, engine: Engine, *, user_id: uuid.UUID, kb_id: uuid.UUID
) -> list[Any]:
    analyzer = RetrievalAnalyzer(
        terms=KEYWORD_TERMS, analyzer_id=kb_keyword_analyzer_version(engine, kb_id)
    )
    async with api_session(api_url) as session:
        return await run_search(
            session,
            user_id=user_id,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query=QUERY,
            embedder=RetrievalEmbedder(vector=(1.0,)),
            analyzer=analyzer,
        )


def _assistant_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [message for message in body["messages"] if message["role"] == "assistant"]


def _require_published(engine: Engine, job_id: uuid.UUID) -> uuid.UUID:
    job = job_row(engine, job_id)
    assert job["status"] == "READY", job
    assert job["error_code"] is None
    assert job["generation_id"] is not None
    return uuid.UUID(str(job["generation_id"]))


@pytest.mark.anyio
async def test_demo_full_chain_upload_ask_update_delete(
    chain_schema: Engine,
    role_test_databases: RoleTestDatabases,
    worker_sessions: Any,
    blob_root: Path,
    chain_settings: Settings,
) -> None:
    storage = DocumentBlobStore(blob_root)
    username = unique_username()
    user_id = seed_user(chain_schema, username=username)
    kb_id = seed_kb(chain_schema, name=unique_name())
    seed_member(chain_schema, kb_id=kb_id, user_id=user_id, role="OWNER")

    context = _context(user_id, ORGANIZATION_ID)

    async with api_client(chain_settings) as client:
        csrf = await login_csrf(client, username)

        # --- 1. 两种格式上传并真实入库 READY -------------------------------
        md_accepted = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            title=MD_TITLE,
            content=MD_V2,
            csrf=csrf,
        )
        assert md_accepted.status_code == 202, md_accepted.text
        md_document_id = uuid.UUID(md_accepted.json()["documentId"])
        md_version_v1 = uuid.UUID(md_accepted.json()["versionId"])
        md_job_id = uuid.UUID(md_accepted.json()["jobId"])
        # 受理后解析/索引尚未完成：job 仍为 QUEUED，文档尚无 active 版本。
        assert job_row(chain_schema, md_job_id)["status"] == "QUEUED"
        assert document_row(chain_schema, md_document_id)["active_version_id"] is None

        assert publish(chain_schema, worker_sessions, storage, md_job_id) == iw.PROCESS_STATUS_READY
        _require_published(chain_schema, md_job_id)

        pdf_accepted = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            title=PDF_TITLE,
            content=PDF_TEXT,
            filename="cafeteria.pdf",
            content_type="application/pdf",
            csrf=csrf,
        )
        assert pdf_accepted.status_code == 202, pdf_accepted.text
        pdf_document_id = uuid.UUID(pdf_accepted.json()["documentId"])
        pdf_job_id = uuid.UUID(pdf_accepted.json()["jobId"])
        assert (
            publish(chain_schema, worker_sessions, storage, pdf_job_id)
            == iw.PROCESS_STATUS_READY
        )
        _require_published(chain_schema, pdf_job_id)

        md_doc = document_row(chain_schema, md_document_id)
        pdf_doc = document_row(chain_schema, pdf_document_id)
        assert md_doc["lifecycle_status"] == "READY"
        assert md_doc["active_version_id"] == md_version_v1
        assert pdf_doc["lifecycle_status"] == "READY"
        assert pdf_doc["source_type"] == "pdf"

        md_locators = chunk_locators(chain_schema, md_document_id)
        assert md_locators and all(
            locator["source_type"] == "markdown"
            and locator["locator_version"] == 1
            and isinstance(locator["start_line"], int)
            and isinstance(locator["end_line"], int)
            and "pages" not in locator
            for locator in md_locators
        )
        pdf_locators = chunk_locators(chain_schema, pdf_document_id)
        assert pdf_locators and all(
            locator["source_type"] == "pdf"
            and locator["locator_version"] == 2
            and locator["pages"]
            and "start_line" not in locator
            for locator in pdf_locators
        )

        # --- 2. 真实问答 API 返回引用、内联标记与引用详情 -------------------
        generator = ChainGenerator()
        async with demo_qa_client(
            chain_settings, context=context, generator=generator, engine=chain_schema, kb_id=kb_id
        ) as qa:
            conversation_id = await _create_conversation(qa, kb_id)

            first = await _ask(qa, conversation_id, QUERY)
            assert first.status_code == 200, first.text
            first_body = first.json()
            first_citations = first_body["citations"]
            source_types = {
                citation["locator"]["source_type"] for citation in first_citations
            }
            assert source_types == {"markdown", "pdf"}, first_citations
            for citation in first_citations:
                uuid.UUID(citation["citationId"])
                assert citation["version"] == 1
                assert citation["isCurrentVersion"] is True
                detail = await qa.get(f"/api/v1/citations/{citation['citationId']}")
                assert detail.status_code == 200, detail.text
                assert detail.json()["documentTitle"] == citation["documentTitle"]
            assert first_body["answer"].startswith("制度规定。")
            assert "[1]" in first_body["answer"]
            md_citation_v1 = next(
                citation
                for citation in first_citations
                if citation["locator"]["source_type"] == "markdown"
            )
            pdf_citation = next(
                citation
                for citation in first_citations
                if citation["locator"]["source_type"] == "pdf"
            )
            assert md_citation_v1["documentTitle"] == MD_TITLE
            assert pdf_citation["documentTitle"] == PDF_TITLE
            assert generator.rewrite_calls == 0

            # --- 3. 追问经受限改写后再检索 ---------------------------------
            follow_up = await _ask(qa, conversation_id, FOLLOW_UP)
            assert follow_up.status_code == 200, follow_up.text
            assert generator.rewrite_calls == 1
            assert generator.answer_calls == 2

            # --- 4. 更新版本后历史旧引用被标为非当前 -----------------------
            current_version_id = document_row(chain_schema, md_document_id)["active_version_id"]
            assert current_version_id == md_version_v1
            updated = await upload_version(
                client,
                md_document_id,
                idempotency_key=unique_key(),
                expected_version_id=md_version_v1,
                title=MD_TITLE,
                content=MD_V3,
                csrf=csrf,
            )
            assert updated.status_code == 202, updated.text
            md_version_v2 = uuid.UUID(updated.json()["versionId"])
            assert publish(
                chain_schema, worker_sessions, storage, uuid.UUID(updated.json()["jobId"])
            ) == iw.PROCESS_STATUS_READY
            assert document_row(chain_schema, md_document_id)["active_version_id"] == md_version_v2

            history = await qa.get(f"/api/v1/conversations/{conversation_id}/messages")
            assert history.status_code == 200, history.text
            assistant = _assistant_messages(history.json())
            assert len(assistant) == 2
            old_md = next(
                citation
                for citation in assistant[0]["citations"]
                if citation["locator"]["source_type"] == "markdown"
            )
            assert old_md["citationId"] == md_citation_v1["citationId"]
            assert old_md["version"] == 1
            assert old_md["isCurrentVersion"] is False
            assert "[1]" in assistant[0]["content"]
            detail = await qa.get(f"/api/v1/citations/{md_citation_v1['citationId']}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["version"] == 1
            assert detail.json()["isCurrentVersion"] is False

            third = await _ask(qa, conversation_id, STANDALONE)
            assert third.status_code == 200, third.text
            third_md = next(
                citation
                for citation in third.json()["citations"]
                if citation["locator"]["source_type"] == "markdown"
            )
            assert third_md["version"] == 2
            assert third_md["isCurrentVersion"] is True

            # --- 5. 删除后历史/引用隐藏且检索不再返回该文档 -----------------
            deleted = await delete_document_request(client, md_document_id, csrf=csrf)
            assert deleted.status_code == 204, deleted.text
            assert document_row(chain_schema, md_document_id)["deleted_at"] is not None

            hidden = await qa.get(f"/api/v1/conversations/{conversation_id}/messages")
            assert hidden.status_code == 200, hidden.text
            assert _assistant_messages(hidden.json()) == []
            gone = await qa.get(f"/api/v1/citations/{md_citation_v1['citationId']}")
            assert gone.status_code == 404, gone.text
            assert gone.json()["code"] == CODE_CITATION_NOT_FOUND

    candidates = await _search(
        role_test_databases.api_url, chain_schema, user_id=user_id, kb_id=kb_id
    )
    assert candidates
    assert all(candidate.document_id != md_document_id for candidate in candidates)
    assert {candidate.document_id for candidate in candidates} == {pdf_document_id}
    assert any(candidate.keyword_rank is not None for candidate in candidates)
    assert generator.answer_calls == 3
