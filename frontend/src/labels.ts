/** 服务端枚举值到简体中文的静态映射，以及时间与定位信息的展示辅助。 */

import type { JobStatus, KbRole, LifecycleStatus, SourceType, VersionStatus } from "./api/types";

const KB_ROLE_LABELS: Record<KbRole, string> = {
  OWNER: "所有者",
  EDITOR: "编辑者",
  READER: "读者",
};

const LIFECYCLE_LABELS: Record<LifecycleStatus, string> = {
  CREATED: "已受理",
  INDEXING: "入库中",
  READY: "可用",
  FAILED: "失败",
  DELETED: "已删除",
};

const VERSION_LABELS: Record<VersionStatus, string> = {
  PENDING: "待处理",
  READY: "就绪",
  FAILED: "失败",
  NEEDS_OCR: "需 OCR（无文本层）",
};

const JOB_LABELS: Record<JobStatus, string> = {
  QUEUED: "排队中",
  PARSING: "解析中",
  CHUNKING: "切分中",
  EMBEDDING: "向量化中",
  INDEXING: "索引中",
  READY: "已完成",
  FAILED: "已失败",
  CANCELLED: "已取消",
};

const SOURCE_TYPE_LABELS: Record<SourceType, string> = {
  markdown: "Markdown",
  pdf: "PDF",
  docx: "DOCX",
  web: "网页",
};

/** 后端 `ingest_job.error_code` 的静态诊断码映射；未知码原样展示并标注“诊断码”。 */
const JOB_ERROR_LABELS: Record<string, string> = {
  HANDLER_NOT_READY: "任务已接收但处理未就绪",
  LEGACY_JOB_UNSUPPORTED: "旧任务不受当前管线支持",
  DELIVERY_UNCONFIRMED: "投递未确认，已停止重投",
  UNSUPPORTED_EVENT_TYPE: "不支持的事件类型",
  DOCUMENT_DELETED: "文档已删除，任务已取消",
  PIPELINE_IDENTITY_UNAVAILABLE: "索引身份不可用",
  PIPELINE_PROFILE_MISMATCH: "索引配置不匹配",
  PIPELINE_SOURCE_UNSUPPORTED: "不支持的来源格式",
  PIPELINE_BLOB_INVALID: "原始文件校验失败",
  PIPELINE_SOURCE_HASH_MISMATCH: "文件摘要不一致",
  PIPELINE_CONTENT_EMPTY: "未提取到正文",
  PIPELINE_CHUNK_FAILED: "切分失败",
  PIPELINE_EMBEDDING_FAILED: "向量化失败",
  PIPELINE_EMBEDDING_REJECTED: "向量输入被拒绝",
  PIPELINE_STAGING_INVALID: "暂存索引不完整",
  PIPELINE_PUBLISH_CONFLICT: "发布冲突，版本已变化",
  PIPELINE_UNSUPPORTED_UPDATE: "不支持的更新范围",
  PIPELINE_STALE_EXPECTED: "期望版本已被超越",
  PIPELINE_PARSE_TIMEOUT: "解析超时",
  PIPELINE_PARSE_FAILED: "解析失败",
  PIPELINE_PDF_ENCRYPTED: "PDF 已加密",
  PIPELINE_PDF_TOO_MANY_PAGES: "PDF 页数超过上限",
  PIPELINE_PDF_INVALID: "PDF 文件损坏",
  PIPELINE_DOCX_UNSUPPORTED: "DOCX 含不支持的结构（如嵌套表格）",
  PIPELINE_DOCX_INVALID: "DOCX 文件损坏或超出安全上限",
  PIPELINE_NEEDS_OCR: "没有可提取文本层，需要 OCR",
  PIPELINE_DB_ERROR: "入库数据库错误",
  PIPELINE_RETRY_EXHAUSTED: "处理中断已耗尽重试",
};

/** 任务是否还在推进；终态不再触发轮询。 */
const TERMINAL_JOB_STATUSES = new Set<JobStatus>(["READY", "FAILED", "CANCELLED"]);

export function kbRoleLabel(role: KbRole): string {
  return KB_ROLE_LABELS[role] ?? role;
}

export function lifecycleLabel(status: LifecycleStatus): string {
  return LIFECYCLE_LABELS[status] ?? status;
}

export function versionLabel(status: VersionStatus): string {
  return VERSION_LABELS[status] ?? status;
}

export function jobLabel(status: JobStatus): string {
  return JOB_LABELS[status] ?? status;
}

export function sourceTypeLabel(sourceType: SourceType): string {
  return SOURCE_TYPE_LABELS[sourceType] ?? sourceType;
}

/** 引用版本标识：旧版本引用仍可展示，但必须标清文档已更新、不能当作当前事实。 */
export function citationVersionLabel(isCurrentVersion: boolean): string {
  return isCurrentVersion ? "当前版本" : "旧版本（已更新）";
}

export function jobErrorLabel(errorCode: string | null): string {
  if (errorCode === null || errorCode === "") return "";
  const known = JOB_ERROR_LABELS[errorCode];
  return known !== undefined ? known : `诊断码 ${errorCode}`;
}

export function isTerminalJobStatus(status: JobStatus): boolean {
  return TERMINAL_JOB_STATUSES.has(status);
}

const LIFECYCLE_BADGE_VARIANTS: Record<LifecycleStatus, "secondary" | "outline" | "destructive"> = {
  CREATED: "outline",
  INDEXING: "outline",
  READY: "secondary",
  FAILED: "destructive",
  DELETED: "destructive",
};

/** 文档生命周期状态对应的徽标样式；未知值退回中性样式，不猜测语义。 */
export function lifecycleBadgeVariant(
  status: LifecycleStatus,
): "secondary" | "outline" | "destructive" {
  return LIFECYCLE_BADGE_VARIANTS[status] ?? "outline";
}

export type DayGroupLabel = "今天" | "昨天" | "更早";

function localDayNumber(value: Date): number {
  return Math.floor(
    new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime() / 86_400_000,
  );
}

/** 会话时间分组：按最近消息时间（无消息回退创建时间）落在今天/昨天/更早。 */
export function dayGroupLabel(value: string | null): DayGroupLabel {
  if (value === null || value === "") return "更早";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "更早";
  const days = localDayNumber(new Date()) - localDayNumber(parsed);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  return "更早";
}

export function formatTime(value: string | null): string {
  if (value === null || value === "") return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

export type LocatorView =
  | { kind: "lines"; text: string }
  | { kind: "pages"; text: string }
  | { kind: "blocks"; text: string }
  | { kind: "raw"; text: string };

/**
 * 只按 locator_version 解释已知键集合：
 * v1 是 Markdown 块级 1-based 闭区间行范围，v2 是 PDF 页号，
 * v3 是 DOCX 的段落/表格行位置；v4 是网页的原文 URL 与块 ordinal；其余原样展示，绝不猜。
 */
export function describeLocator(locator: Record<string, unknown> | null | undefined): LocatorView {
  if (locator === null || locator === undefined) {
    return { kind: "raw", text: "服务端未返回定位信息" };
  }
  const version = locator.locator_version;
  if (version === 1) {
    const start = locator.start_line;
    const end = locator.end_line;
    if (typeof start === "number" && typeof end === "number") {
      return { kind: "lines", text: `第 ${start}–${end} 行（块级粗粒度，1 起）` };
    }
  }
  if (version === 2) {
    const pages: unknown = locator.pages;
    if (Array.isArray(pages)) {
      const numbers = pages.filter((page): page is number => typeof page === "number");
      if (numbers.length > 0) {
        return { kind: "pages", text: `第 ${numbers.join("、")} 页` };
      }
      return { kind: "raw", text: "PDF 页定位为空" };
    }
  }
  if (version === 3) {
    const segments: unknown = locator.segments;
    if (Array.isArray(segments)) {
      const parts = new Set<string>();
      for (const segment of segments) {
        if (typeof segment !== "object" || segment === null) continue;
        const record = segment as Record<string, unknown>;
        if (typeof record.paragraph_index === "number") {
          parts.add(`第 ${record.paragraph_index} 段`);
        } else if (
          typeof record.table_index === "number" &&
          typeof record.row_index === "number"
        ) {
          parts.add(`第 ${record.table_index} 个表格第 ${record.row_index} 行`);
        }
      }
      if (parts.size > 0) {
        return { kind: "blocks", text: [...parts].join("；") };
      }
      return { kind: "raw", text: "DOCX 定位为空" };
    }
  }
  if (version === 4) {
    const parts: string[] = [];
    if (typeof locator.source_url === "string" && locator.source_url !== "") {
      parts.push(locator.source_url);
    }
    const segments: unknown = locator.segments;
    if (Array.isArray(segments)) {
      const ordinals = new Set<number>();
      for (const segment of segments) {
        if (typeof segment !== "object" || segment === null) continue;
        const ordinal = (segment as Record<string, unknown>).block_ordinal;
        if (typeof ordinal === "number") ordinals.add(ordinal);
      }
      if (ordinals.size > 0) {
        parts.push(`第 ${[...ordinals].join("、")} 块`);
      }
    }
    if (parts.length > 0) {
      return { kind: "blocks", text: parts.join(" · ") };
    }
    return { kind: "raw", text: "网页定位为空" };
  }
  return { kind: "raw", text: JSON.stringify(locator, null, 2) };
}
