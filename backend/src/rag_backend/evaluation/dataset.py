"""开发评估题集与语料清单的 schema 与离线校验。

设计约束：

- gold 引用只绑定**来源版本**与**原文行区间或页码**（外加解析器版本），不绑定 chunk UUID；
  校验时用同一解析器重放样本文件，确认引文确实落在声明的区间/页。
- 校验器不联网、不读环境文件、不调用模型；PDF 校验才延迟导入 ``pypdf``。
- 开发题集（``datasetKind="dev"``）用于结构自检，不冒充留出集或质量评分。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

Category = Literal["single_document", "cross_document", "unanswerable", "no_permission"]
AnswerBehavior = Literal["answer", "refuse"]
UnavailableReason = Literal["no_permission", "deleted"]

CATEGORIES: tuple[Category, ...] = (
    "single_document",
    "cross_document",
    "unanswerable",
    "no_permission",
)
MIN_DEV_QUESTIONS = 30

# 题集自身不得泄露的最小“实质行”长度；短行（标题、口令等）不参与泄漏判定。
_LEAK_MIN_LINE_CHARS = 12


class DatasetValidationError(Exception):
    """题集或语料不满足契约时抛出；消息只含静态的题集位置与原因。"""


class _Model(BaseModel):
    """严格 camelCase 模型：外部 JSON 字段 camelCase，未知字段直接拒绝。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CorpusVersion(_Model):
    """一个来源版本：绑定可复现样本文件、来源类型、解析器版本与状态。"""

    version: int = Field(ge=1)
    file: str = Field(min_length=1)
    source_type: Literal["markdown", "pdf"]
    parser_version: str = Field(min_length=1)
    status: Literal["active", "superseded", "deleted"]


class CorpusDocument(_Model):
    """一个逻辑文档的版本集合；``current_version`` 为空表示已逻辑删除。"""

    current_version: int | None = None
    versions: list[CorpusVersion] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_versions(self) -> CorpusDocument:
        seen: set[int] = set()
        for entry in self.versions:
            if entry.version in seen:
                raise ValueError(f"重复的版本号 {entry.version}")
            seen.add(entry.version)
        active = [entry for entry in self.versions if entry.status == "active"]
        if len(active) > 1:
            raise ValueError("同一文档不能有多个 active 版本")
        if self.current_version is None:
            if active:
                raise ValueError("currentVersion 为空但存在 active 版本")
            return self
        if self.current_version not in seen:
            raise ValueError(f"currentVersion={self.current_version} 不在 versions 中")
        entry = next(item for item in self.versions if item.version == self.current_version)
        if entry.status != "active":
            raise ValueError("currentVersion 必须指向 active 版本")
        return self


class KnowledgeBaseConfig(_Model):
    documents: dict[str, CorpusDocument] = Field(min_length=1)


class RoleConfig(_Model):
    knowledge_bases: list[str] = Field(default_factory=list)


class CorpusManifest(_Model):
    """语料清单：KB、文档版本与角色可访问的 KB 集合。"""

    manifest_version: str = Field(min_length=1)
    note: str = ""
    knowledge_bases: dict[str, KnowledgeBaseConfig] = Field(min_length=1)
    roles: dict[str, RoleConfig] = Field(min_length=1)

    def version_entry(self, kb_id: str, document_id: str) -> CorpusDocument:
        if kb_id not in self.knowledge_bases:
            raise DatasetValidationError(f"未知 knowledgeBase：{kb_id}")
        documents = self.knowledge_bases[kb_id].documents
        if document_id not in documents:
            raise DatasetValidationError(f"未知 document：{kb_id}/{document_id}")
        return documents[document_id]

    def resolve(self, kb_id: str, document_id: str, version: int) -> CorpusVersion:
        document = self.version_entry(kb_id, document_id)
        for entry in document.versions:
            if entry.version == version:
                return entry
        raise DatasetValidationError(f"未登记版本：{kb_id}/{document_id}@v{version}")

    def role_can_access_kb(self, role: str, kb_id: str) -> bool:
        if role not in self.roles:
            raise DatasetValidationError(f"未知 role：{role}")
        return kb_id in self.roles[role].knowledge_bases


class GoldLocator(_Model):
    """原文定位：Markdown 用标题路径 + 1-based 行区间；PDF 用 1-based 页号。"""

    source_type: Literal["markdown", "pdf"]
    parser_version: str = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> GoldLocator:
        if self.source_type == "markdown":
            if self.page is not None:
                raise ValueError("Markdown 定位不能包含 page")
            if self.start_line is None or self.end_line is None:
                raise ValueError("Markdown 定位必须包含 startLine 与 endLine")
            if self.start_line < 1 or self.end_line < self.start_line:
                raise ValueError("Markdown 行区间非法")
        else:
            if self.start_line is not None or self.end_line is not None:
                raise ValueError("PDF 定位不能包含行号")
            if self.heading_path:
                raise ValueError("PDF 没有标题路径，headingPath 必须为空")
            if self.page is None or self.page < 1:
                raise ValueError("PDF 定位必须包含 1-based page")
        return self


class GoldSpan(_Model):
    """一个 gold 引用：绑定（KB、文档、版本）与原文区间/页，不含 chunk UUID。"""

    kb_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    quote: str = Field(min_length=1)
    locator: GoldLocator


class ConversationTurn(_Model):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)


class QuestionScope(_Model):
    role: str = Field(min_length=1)
    kb_ids: list[str] = Field(min_length=1)


class EvaluationQuestion(_Model):
    """一道开发评估题：分类、权限范围、预期行为与 gold 引用。"""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: Category
    scope: QuestionScope
    question: str = Field(min_length=1)
    standalone_question: str | None = None
    history: list[ConversationTurn] = Field(default_factory=list)
    expected_behavior: AnswerBehavior
    gold_answer_points: list[str] = Field(default_factory=list)
    gold_source_spans: list[GoldSpan] = Field(default_factory=list)
    unavailable_document_ids: list[str] = Field(default_factory=list)
    unavailable_reason: UnavailableReason | None = None
    distractors: list[GoldSpan] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> EvaluationQuestion:
        if self.category in ("single_document", "cross_document"):
            if self.expected_behavior != "answer":
                raise ValueError("可回答题必须 expectedBehavior=answer")
            if not self.gold_source_spans:
                raise ValueError("可回答题必须至少有一个 goldSourceSpans")
            if not self.gold_answer_points:
                raise ValueError("可回答题必须至少有一个 goldAnswerPoints")
            distinct = {(span.kb_id, span.document_id) for span in self.gold_source_spans}
            if self.category == "single_document" and len(distinct) != 1:
                raise ValueError("single_document 的 gold 只能来自一个文档")
            if self.category == "cross_document" and len(distinct) < 2:
                raise ValueError("cross_document 的 gold 必须来自至少两个文档")
            if self.unavailable_document_ids or self.unavailable_reason is not None:
                raise ValueError("可回答题不能声明 unavailable 文档")
        else:
            if self.expected_behavior != "refuse":
                raise ValueError("无答案或无权限题必须 expectedBehavior=refuse")
            if self.gold_source_spans:
                raise ValueError("无答案或无权限题不能有 goldSourceSpans")
            if self.gold_answer_points:
                raise ValueError("无答案或无权限题不能有 goldAnswerPoints")
        if bool(self.unavailable_document_ids) != (self.unavailable_reason is not None):
            raise ValueError("unavailableDocumentIds 与 unavailableReason 必须同时出现或同时为空")
        if self.category == "no_permission" and self.unavailable_reason != "no_permission":
            raise ValueError("no_permission 题必须声明 unavailableReason=no_permission")
        if self.category == "unanswerable" and self.unavailable_reason == "no_permission":
            raise ValueError("unanswerable 题不能声明权限原因")
        if "multi_turn" in self.tags:
            if not self.history:
                raise ValueError("multi_turn 题必须带 history")
            if not self.standalone_question:
                raise ValueError("multi_turn 题必须带 standaloneQuestion")
            if self.standalone_question == self.question:
                raise ValueError("multi_turn 的 standaloneQuestion 必须与原始问题不同")
        if "pdf_page" in self.tags and not any(
            span.locator.source_type == "pdf" for span in self.gold_source_spans
        ):
            raise ValueError("pdf_page 题必须至少有一个 PDF gold span")
        if "version_update" in self.tags and not self.distractors:
            raise ValueError("version_update 题必须给出 superseded 版本 distractor")
        return self


class EvaluationDataset(_Model):
    """开发题集文件；``datasetKind`` 目前只允许 ``dev``，防止开发集冒充留出/测试集。"""

    dataset_kind: Literal["dev"]
    dataset_version: str = Field(min_length=1)
    corpus_manifest: str = Field(min_length=1)
    notes: str = ""
    questions: list[EvaluationQuestion] = Field(min_length=1)


@dataclass(frozen=True)
class ValidationReport:
    """离线校验通过后的汇总；只报告题集自身事实，不含任何模型质量分数。"""

    dataset_kind: str
    dataset_version: str
    total: int
    category_counts: dict[str, int]
    tag_counts: dict[str, int]


def load_manifest(path: Path) -> CorpusManifest:
    return CorpusManifest.model_validate_json(_read_text(path, "语料清单"))


def load_dataset(path: Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(_read_text(path, "题集文件"))


def _read_text(path: Path, label: str) -> str:
    """读取 UTF-8 文本；读取失败统一转为静态题集错误，不泄露绝对路径。"""

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DatasetValidationError(f"{label}不可读：{path.name}") from error


def validate_dataset(
    dataset: EvaluationDataset,
    manifest: CorpusManifest,
    corpus_dir: Path,
) -> ValidationReport:
    """校验题集 schema 之外的全部语料与权限约束，失败时抛 ``DatasetValidationError``。"""

    _assert_unique_ids(dataset)
    _assert_category_coverage(dataset)
    for question in dataset.questions:
        _validate_scope(question, manifest)
        _validate_gold_spans(question, manifest, corpus_dir)
        _validate_distractors(question, manifest, corpus_dir)
        _validate_unavailable_documents(question, manifest, corpus_dir)
    return ValidationReport(
        dataset_kind=dataset.dataset_kind,
        dataset_version=dataset.dataset_version,
        total=len(dataset.questions),
        category_counts=dict(Counter(question.category for question in dataset.questions)),
        tag_counts=dict(Counter(tag for question in dataset.questions for tag in question.tags)),
    )


def _assert_unique_ids(dataset: EvaluationDataset) -> None:
    ids = [question.id for question in dataset.questions]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise DatasetValidationError(f"重复的题目 id：{', '.join(duplicates)}")


def _assert_category_coverage(dataset: EvaluationDataset) -> None:
    if dataset.dataset_kind == "dev" and len(dataset.questions) < MIN_DEV_QUESTIONS:
        raise DatasetValidationError(
            f"开发题集至少需要 {MIN_DEV_QUESTIONS} 道题，当前 {len(dataset.questions)} 道"
        )
    present = {question.category for question in dataset.questions}
    missing = [category for category in CATEGORIES if category not in present]
    if missing:
        raise DatasetValidationError(f"题集缺少分类：{', '.join(missing)}")


def _validate_scope(question: EvaluationQuestion, manifest: CorpusManifest) -> None:
    role = question.scope.role
    if role not in manifest.roles:
        raise DatasetValidationError(f"[{question.id}] 未知 role：{role}")
    for kb_id in question.scope.kb_ids:
        if kb_id not in manifest.knowledge_bases:
            raise DatasetValidationError(f"[{question.id}] 未知 knowledgeBase：{kb_id}")
        if not manifest.role_can_access_kb(role, kb_id):
            raise DatasetValidationError(f"[{question.id}] role {role} 无权访问请求 KB {kb_id}")


def _validate_gold_spans(
    question: EvaluationQuestion, manifest: CorpusManifest, corpus_dir: Path
) -> None:
    for span in question.gold_source_spans:
        _assert_span_scope_and_access(question, span, manifest)
        entry = _resolve_span_entry(question, span, manifest)
        if entry.status != "active":
            raise DatasetValidationError(f"[{question.id}] 可回答题的 gold 必须绑定 active 版本")
        _assert_gold_match(question.id, corpus_dir / entry.file, span, entry)


def _assert_span_scope_and_access(
    question: EvaluationQuestion, span: GoldSpan, manifest: CorpusManifest
) -> None:
    """gold/distractor 必须落在本题请求且角色可访问的 KB 内。"""

    if span.kb_id not in question.scope.kb_ids:
        raise DatasetValidationError(
            f"[{question.id}] span 的 KB {span.kb_id} 不在本题 scope.kbIds 内"
        )
    if not manifest.role_can_access_kb(question.scope.role, span.kb_id):
        raise DatasetValidationError(
            f"[{question.id}] role {question.scope.role} 无权访问 span KB {span.kb_id}"
        )


def _validate_distractors(
    question: EvaluationQuestion, manifest: CorpusManifest, corpus_dir: Path
) -> None:
    for span in question.distractors:
        _assert_span_scope_and_access(question, span, manifest)
        entry = _resolve_span_entry(question, span, manifest)
        if entry.status != "superseded":
            raise DatasetValidationError(f"[{question.id}] distractor 必须绑定 superseded 版本")
        _assert_gold_match(question.id, corpus_dir / entry.file, span, entry)


def _resolve_span_entry(
    question: EvaluationQuestion, span: GoldSpan, manifest: CorpusManifest
) -> CorpusVersion:
    entry = manifest.resolve(span.kb_id, span.document_id, span.version)
    if entry.source_type != span.locator.source_type:
        raise DatasetValidationError(f"[{question.id}] gold span sourceType 与清单不一致")
    if entry.parser_version != span.locator.parser_version:
        raise DatasetValidationError(f"[{question.id}] gold span parserVersion 与清单不一致")
    return entry


def _validate_unavailable_documents(
    question: EvaluationQuestion, manifest: CorpusManifest, corpus_dir: Path
) -> None:
    if not question.unavailable_document_ids:
        return
    haystack = _question_text(question)
    role = question.scope.role
    for document_id in question.unavailable_document_ids:
        kb_id = kb_of_document(manifest, document_id)
        document = manifest.version_entry(kb_id, document_id)
        if question.unavailable_reason == "no_permission":
            if manifest.role_can_access_kb(role, kb_id):
                raise DatasetValidationError(
                    f"[{question.id}] no_permission 文档对 role {role} 可访问"
                )
            if document.current_version is None:
                raise DatasetValidationError(
                    f"[{question.id}] no_permission 文档没有有效版本"
                )
        else:
            if not manifest.role_can_access_kb(role, kb_id):
                raise DatasetValidationError(
                    f"[{question.id}] deletion 题的 KB 对 role {role} 不可访问"
                )
            if document.current_version is not None:
                raise DatasetValidationError(f"[{question.id}] deletion 文档仍有有效版本")
            if not any(entry.status == "deleted" for entry in document.versions):
                raise DatasetValidationError(
                    f"[{question.id}] deletion 文档没有 status=deleted 的版本"
                )
        for entry in document.versions:
            _assert_no_leak(question.id, corpus_dir / entry.file, haystack, document_id)


def _question_text(question: EvaluationQuestion) -> str:
    parts = [question.question, question.standalone_question or ""]
    parts.extend(turn.text for turn in question.history)
    parts.extend(question.gold_answer_points)
    return "\n".join(parts)


def _assert_no_leak(
    question_id: str, path: Path, haystack: str, document_id: str
) -> None:
    for line in substantive_lines(path):
        if line in haystack:
            raise DatasetValidationError(
                f"[{question_id}] 题面包含不可访问文档 {document_id} 的原文，违反不泄漏约束"
            )


def substantive_lines(path: Path) -> list[str]:
    """样本中可能构成泄漏的实质行：够长且不是标题/引用注释。"""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DatasetValidationError(f"语料文件不可读：{path.name}") from error
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < _LEAK_MIN_LINE_CHARS:
            continue
        if line.startswith("#") or line.startswith(">"):
            continue
        lines.append(line)
    return lines


def kb_of_document(manifest: CorpusManifest, document_id: str) -> str:
    matches = [
        kb_id
        for kb_id, config in manifest.knowledge_bases.items()
        if document_id in config.documents
    ]
    if not matches:
        raise DatasetValidationError(f"未知 document：{document_id}")
    if len(matches) > 1:
        raise DatasetValidationError(f"document {document_id} 在多个 KB 中定义，无法唯一解析")
    return matches[0]


def _assert_gold_match(
    question_id: str, path: Path, span: GoldSpan, entry: CorpusVersion
) -> None:
    if not path.is_file():
        raise DatasetValidationError(f"[{question_id}] 语料文件不存在：{path.name}")
    locator = span.locator
    try:
        content = path.read_bytes()
    except OSError as error:
        raise DatasetValidationError(f"[{question_id}] 语料文件不可读：{path.name}") from error
    if locator.source_type == "markdown":
        from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION, parse_markdown

        if entry.parser_version != MARKDOWN_PARSER_VERSION:
            raise DatasetValidationError(
                f"[{question_id}] Markdown 解析器版本漂移："
                f"清单 {entry.parser_version} != 实现 {MARKDOWN_PARSER_VERSION}"
            )
        try:
            parsed = parse_markdown(content)
        except UnicodeDecodeError as error:
            raise DatasetValidationError(
                f"[{question_id}] 语料文件不是有效 UTF-8"
            ) from error
        candidates = [
            block
            for block in parsed.blocks
            if block.heading_path == tuple(locator.heading_path)
            and block.start_line == locator.start_line
            and block.end_line == locator.end_line
        ]
        if not candidates:
            raise DatasetValidationError(f"[{question_id}] 找不到匹配的 Markdown 块定位")
        if not any(span.quote in block.text for block in candidates):
            raise DatasetValidationError(f"[{question_id}] gold 引文不在声明的 Markdown 行区间内")
        return
    from rag_backend.ingestion.pdf_parsing import (
        PDF_PARSER_VERSION,
        PdfParsingError,
        parse_pdf,
    )

    if entry.parser_version != PDF_PARSER_VERSION:
        raise DatasetValidationError(
            f"[{question_id}] PDF 解析器版本漂移："
            f"清单 {entry.parser_version} != 实现 {PDF_PARSER_VERSION}"
        )
    try:
        parsed = parse_pdf(content)
    except ImportError as error:  # pdfplumber 属 dev/worker 组，仅在校验 PDF 时才需要
        raise DatasetValidationError(
            f"[{question_id}] 缺少 pdfplumber，无法校验 PDF gold 引用"
        ) from error
    except PdfParsingError as error:
        raise DatasetValidationError(f"[{question_id}] PDF 解析失败：{error}") from error
    page_blocks = [block for block in parsed.blocks if block.page == locator.page]
    if not page_blocks:
        raise DatasetValidationError(f"[{question_id}] PDF 声明页 {locator.page} 没有可提取文本")
    if not any(span.quote in block.text for block in page_blocks):
        raise DatasetValidationError(f"[{question_id}] gold 引文不在声明的 PDF 页内")


def load_dataset_bundle(
    dataset_path: Path,
) -> tuple[EvaluationDataset, CorpusManifest, Path]:
    """按题集文件内的相对路径加载语料清单，返回 (题集, 清单, 语料目录)。"""

    resolved = dataset_path.resolve()
    if not resolved.is_file():
        raise DatasetValidationError(f"题集文件不存在：{dataset_path.name}")
    dataset = load_dataset(resolved)
    corpus_dir = resolved.parent
    manifest_path = corpus_dir / dataset.corpus_manifest
    if not manifest_path.is_file():
        raise DatasetValidationError(f"语料清单不存在：{dataset.corpus_manifest}")
    manifest = load_manifest(manifest_path)
    return dataset, manifest, manifest_path.parent
