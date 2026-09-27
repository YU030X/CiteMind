/** 已实现端点的类型化封装；路径与请求体字段以实际 schema 为准。 */

import { request } from "./client";
import type {
  AnswerResponse,
  AskGenerationOptions,
  Citation,
  ConversationListResponse,
  ConversationMessagesResponse,
  ConversationSummary,
  CreateConversationResponse,
  DocumentListResponse,
  DocumentSummary,
  DocumentUploadAccepted,
  KnowledgeBaseListResponse,
  KnowledgeBaseResponse,
  MeOverviewResponse,
  MeResponse,
} from "./types";

export const api = {
  me: (): Promise<MeOverviewResponse> => request<MeOverviewResponse>("/me"),

  login: (username: string, password: string): Promise<MeResponse> =>
    request<MeResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  logout: (): Promise<void> => request<void>("/auth/logout", { method: "POST" }),

  listKnowledgeBases: (): Promise<KnowledgeBaseListResponse> =>
    request<KnowledgeBaseListResponse>("/knowledge-bases"),

  createKnowledgeBase: (name: string): Promise<KnowledgeBaseResponse> =>
    request<KnowledgeBaseResponse>("/knowledge-bases", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  /** 冻结契约：GET /knowledge-bases/{kb_id}/documents，无分页。 */
  listDocuments: (kbId: string): Promise<DocumentListResponse> =>
    request<DocumentListResponse>(`/knowledge-bases/${kbId}/documents`),

  /** 冻结契约：GET /documents/{document_id}，返回与列表相同的单个文档对象。 */
  getDocument: (documentId: string): Promise<DocumentSummary> =>
    request<DocumentSummary>(`/documents/${documentId}`),

  uploadDocument: (
    kbId: string,
    form: FormData,
    idempotencyKey: string,
  ): Promise<DocumentUploadAccepted> =>
    request<DocumentUploadAccepted>(`/knowledge-bases/${kbId}/documents`, {
      method: "POST",
      body: form,
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  uploadDocumentVersion: (
    documentId: string,
    form: FormData,
    idempotencyKey: string,
  ): Promise<DocumentUploadAccepted> =>
    request<DocumentUploadAccepted>(`/documents/${documentId}/versions`, {
      method: "POST",
      body: form,
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  deleteDocument: (documentId: string): Promise<void> =>
    request<void>(`/documents/${documentId}`, { method: "DELETE" }),

  /** 冻结契约：GET /conversations，无分页。 */
  listConversations: (): Promise<ConversationListResponse> =>
    request<ConversationListResponse>("/conversations"),

  createConversation: (kbIds: string[]): Promise<CreateConversationResponse> =>
    request<CreateConversationResponse>("/conversations", {
      method: "POST",
      body: JSON.stringify({ kbIds }),
    }),

  /** 改名/置顶：至少提供 title 或 pinned 之一；返回更新后的会话摘要。 */
  updateConversation: (
    conversationId: string,
    changes: { title?: string; pinned?: boolean },
  ): Promise<ConversationSummary> =>
    request<ConversationSummary>(`/conversations/${conversationId}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    }),

  /** 逻辑删除：成功与重复删除都返回 204；越权或不存在返回 404。 */
  deleteConversation: (conversationId: string): Promise<void> =>
    request<void>(`/conversations/${conversationId}`, { method: "DELETE" }),

  listMessages: (conversationId: string): Promise<ConversationMessagesResponse> =>
    request<ConversationMessagesResponse>(`/conversations/${conversationId}/messages`),

  /** 追问：可携带服务端白名单内的模型与思考选项；省略字段等价于服务端默认（关闭思考）。 */
  ask: (
    conversationId: string,
    question: string,
    requestId: string,
    options: AskGenerationOptions,
  ): Promise<AnswerResponse> =>
    request<AnswerResponse>(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        question,
        requestId,
        model: options.model,
        thinking: { type: options.thinking },
        ...(options.thinking === "enabled" && options.reasoningEffort !== undefined
          ? { reasoningEffort: options.reasoningEffort }
          : {}),
      }),
    }),

  getCitation: (citationId: string): Promise<Citation> =>
    request<Citation>(`/citations/${citationId}`),
};
