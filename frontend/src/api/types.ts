/** 后端 DTO 的只读类型。字段名与后端 camelCase 序列化保持一致，不做重命名映射。 */

export type KbRole = "OWNER" | "EDITOR" | "READER";

export type JobStatus =
  | "QUEUED"
  | "PARSING"
  | "CHUNKING"
  | "EMBEDDING"
  | "INDEXING"
  | "READY"
  | "FAILED"
  | "CANCELLED";

export type VersionStatus = "PENDING" | "READY" | "FAILED" | "NEEDS_OCR";

export type LifecycleStatus = "CREATED" | "INDEXING" | "READY" | "FAILED" | "DELETED";

export type SourceType = "markdown" | "pdf" | "docx" | "web";

export interface UserSummary {
  id: string;
  username: string;
  isAdmin: boolean;
  organizationId: string;
}

export interface MeResponse {
  user: UserSummary;
  csrfToken: string;
  generation: GenerationCapability;
}

/** 只读生成能力事实：服务端白名单内的模型与各自的思考选项，以及默认组合。 */
export type ReasoningEffort = "low" | "high" | "max";

export interface ThinkingCapability {
  supported: boolean;
  efforts: ReasoningEffort[];
  defaultEffort: ReasoningEffort | null;
}

export interface ModelCapability {
  id: string;
  thinking: ThinkingCapability;
}

export interface GenerationCapability {
  enabled: boolean;
  defaultModel: string;
  defaultThinking: "enabled" | "disabled";
  models: ModelCapability[];
}

export interface MeOverviewResponse extends MeResponse {
  knowledgeBases: { id: string; name: string; role: KbRole }[];
}

export interface KnowledgeBaseSummary {
  id: string;
  name: string;
  role: KbRole;
  aclRevision: number;
}

export interface KnowledgeBaseListResponse {
  knowledgeBases: KnowledgeBaseSummary[];
}

export interface KnowledgeBaseResponse {
  id: string;
  name: string;
  role: KbRole;
  aclRevision: number;
  kbRevision: number;
}

export interface VersionSummary {
  id: string;
  versionNo: number;
  status: VersionStatus;
}

export interface JobSummary {
  id: string;
  status: JobStatus;
  errorCode: string | null;
}

/** 文档列表与单文档读取共用的对象；当前新增端点无分页。 */
export interface DocumentSummary {
  id: string;
  title: string;
  sourceType: SourceType;
  lifecycleStatus: LifecycleStatus;
  activeVersion: VersionSummary | null;
  latestVersion: VersionSummary | null;
  latestJob: JobSummary | null;
  createdAt: string;
  updatedAt: string;
}

export interface DocumentListResponse {
  documents: DocumentSummary[];
}

export interface DocumentUploadAccepted {
  documentId: string;
  versionId: string;
  jobId: string;
}

export interface ConversationSummary {
  id: string;
  title: string | null;
  pinned: boolean;
  kbIds: string[];
  createdAt: string;
  lastMessageAt: string | null;
}

export interface ConversationListResponse {
  conversations: ConversationSummary[];
}

export interface CreateConversationResponse {
  conversationId: string;
  kbIds: string[];
  createdAt: string;
}

/** locator 的键集合由后端 locator_version 决定，前端不做结构假设。 */
export interface Citation {
  citationId: string;
  displayLabel: string;
  documentTitle: string;
  version: number;
  locator: Record<string, unknown>;
  quote: string;
  /** 引用版本是否仍是文档当前版本；否表示文档已更新，只能作为历史展示。 */
  isCurrentVersion: boolean;
}

export interface ConversationMessage {
  messageId: string;
  role: "user" | "assistant";
  content: string;
  queryRunId: string | null;
  createdAt: string;
  citations: Citation[];
}

export interface ConversationMessagesResponse {
  conversationId: string;
  messages: ConversationMessage[];
}

/** 追问请求可选的模型与思考选项；服务端只接受白名单枚举，未知组合返回 422。 */
export interface AskGenerationOptions {
  model: string;
  thinking: "enabled" | "disabled";
  reasoningEffort?: ReasoningEffort;
}

export interface AnswerUsage {
  localInputTokens: number | null;
  inputTokenBudget: number;
  outputTokenBudget: number;
  providerPromptTokens: number | null;
  providerCompletionTokens: number | null;
}

export interface AnswerResponse {
  conversationId: string;
  messageId: string;
  queryRunId: string;
  answer: string;
  citations: Citation[];
  insufficientEvidence: boolean;
  degradedStages: string[];
  followUp: string | null;
  usage: AnswerUsage;
}
