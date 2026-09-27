/**
 * 工作台状态与动作。
 *
 * - 登录只在内存中保存 `csrfToken`，不写入任何持久化存储。
 * - 任何 401 都由客户端回调在这里清空会话并回到登录界面。
 * - 资料列表仅在存在非终态任务时按短周期轮询；切换知识库、注销与卸载都会清掉定时器，
 *   并用递增的请求序号丢弃过时响应，避免串页。
 */

import { reactive } from "vue";

import { ApiError, describeError, setCsrfToken, setUnauthorizedHandler } from "../api/client";
import { api } from "../api/endpoints";
import type {
  AskGenerationOptions,
  Citation,
  ConversationMessage,
  ConversationSummary,
  DocumentSummary,
  GenerationCapability,
  KnowledgeBaseSummary,
  ModelCapability,
  ReasoningEffort,
  UserSummary,
} from "../api/types";
import { isTerminalJobStatus } from "../labels";

const DOCUMENTS_POLL_INTERVAL_MS = 5000;

interface Session {
  user: UserSummary;
  csrfToken: string;
}

export const state = reactive({
  booting: true,
  bootError: "",
  sessionExpiredNotice: "",

  session: null as Session | null,
  // 只读的生成能力事实（白名单模型与思考选项），来自登录/`GET /me`。
  generation: null as GenerationCapability | null,
  // 当前选择的模型与思考选项；只在内存里，登录后按服务端默认初始化。
  generationModel: "",
  generationThinking: "disabled" as "enabled" | "disabled",
  generationEffort: "high" as ReasoningEffort,
  loggingIn: false,
  loginError: "",
  loggingOut: false,
  logoutError: "",

  knowledgeBases: [] as KnowledgeBaseSummary[],
  knowledgeBasesLoading: false,
  knowledgeBasesError: "",
  createKnowledgeBasePending: false,
  createKnowledgeBaseError: "",

  activeKbId: "",
  documents: [] as DocumentSummary[],
  documentsLoading: false,
  documentsError: "",

  conversations: [] as ConversationSummary[],
  conversationsLoading: false,
  conversationsError: "",
  newConversationPending: false,
  newConversationError: "",
  conversationActionPending: false,
  conversationActionError: "",

  activeConversationId: "",
  messages: [] as ConversationMessage[],
  messagesLoading: false,
  messagesError: "",
  asking: false,
  askError: "",

  citation: null as Citation | null,
  citationLoading: false,
  citationError: "",

  lastFollowUp: "",
});

let documentsTimer: number | null = null;
let documentsToken = 0;
let messagesToken = 0;
let citationToken = 0;

function stopDocumentsPolling(): void {
  if (documentsTimer !== null) {
    window.clearInterval(documentsTimer);
    documentsTimer = null;
  }
}

function documentsHavePendingWork(): boolean {
  return state.documents.some((document) => {
    const job = document.latestJob;
    if (job !== null && !isTerminalJobStatus(job.status)) return true;
    if (document.latestVersion?.status === "PENDING") return true;
    return document.lifecycleStatus === "CREATED" || document.lifecycleStatus === "INDEXING";
  });
}

function scheduleDocumentsPolling(): void {
  stopDocumentsPolling();
  if (state.session === null || state.activeKbId === "") return;
  if (!documentsHavePendingWork()) return;
  documentsTimer = window.setInterval(() => {
    void loadDocuments(state.activeKbId, true);
  }, DOCUMENTS_POLL_INTERVAL_MS);
}

function resetConversationSelection(): void {
  messagesToken += 1;
  citationToken += 1;
  state.activeConversationId = "";
  state.messages = [];
  state.messagesLoading = false;
  state.messagesError = "";
  state.asking = false;
  state.askError = "";
  state.citation = null;
  state.citationLoading = false;
  state.citationError = "";
  state.lastFollowUp = "";
}

function resetSession(): void {
  stopDocumentsPolling();
  documentsToken += 1;
  resetConversationSelection();
  state.session = null;
  state.generation = null;
  state.generationModel = "";
  state.generationThinking = "disabled";
  state.generationEffort = "high";
  setCsrfToken(null);
  state.knowledgeBases = [];
  state.knowledgeBasesLoading = false;
  state.knowledgeBasesError = "";
  state.createKnowledgeBaseError = "";
  state.activeKbId = "";
  state.documents = [];
  state.documentsLoading = false;
  state.documentsError = "";
  state.conversations = [];
  state.conversationsLoading = false;
  state.conversationsError = "";
  state.newConversationError = "";
}

setUnauthorizedHandler(() => {
  const hadSession = state.session !== null;
  resetSession();
  if (hadSession) state.sessionExpiredNotice = "登录状态已失效，请重新登录";
});

// --- 会话 ---------------------------------------------------------------------

function applySession(
  user: UserSummary,
  csrfToken: string,
  generation: GenerationCapability,
): void {
  state.session = { user, csrfToken };
  setCsrfToken(csrfToken);
  state.generation = generation;
  applyGenerationDefaults(generation);
}

/** 用服务端默认组合初始化选择器；模型或默认强度缺失时退回已知安全值。 */
function applyGenerationDefaults(generation: GenerationCapability): void {
  state.generationModel = generation.defaultModel;
  state.generationThinking = generation.defaultThinking;
  const model = generation.models.find((item) => item.id === generation.defaultModel);
  state.generationEffort = model?.thinking.defaultEffort ?? "high";
}

/** 当前选择对应的能力事实；模型不在白名单内时为 null，UI 据此只读展示。 */
export function activeModelCapability(): ModelCapability | null {
  const capability = state.generation;
  if (capability === null) return null;
  return capability.models.find((item) => item.id === state.generationModel) ?? null;
}

/** 切换模型：不接受白名单外模型；不支持思考的模型强制关闭思考。 */
export function selectGenerationModel(modelId: string): void {
  const capability = state.generation;
  if (capability === null) return;
  const model = capability.models.find((item) => item.id === modelId);
  if (model === undefined) return;
  state.generationModel = model.id;
  if (!model.thinking.supported) {
    state.generationThinking = "disabled";
    return;
  }
  state.generationEffort = model.thinking.defaultEffort ?? state.generationEffort;
}

/** 切换思考开关；不支持思考的模型不接受开启。 */
export function selectGenerationThinking(mode: "enabled" | "disabled"): void {
  if (mode === "enabled" && activeModelCapability()?.thinking.supported !== true) return;
  state.generationThinking = mode;
}

/** 切换思考强度；只接受当前模型声明的枚举。 */
export function selectGenerationEffort(effort: ReasoningEffort): void {
  const capability = activeModelCapability();
  if (capability === null || !capability.thinking.efforts.includes(effort)) return;
  state.generationEffort = effort;
}

async function loadInitialWorkspace(): Promise<void> {
  await loadKnowledgeBases();
  if (state.activeKbId === "" && state.knowledgeBases.length > 0) {
    selectKnowledgeBase(state.knowledgeBases[0].id);
  }
  void loadConversations();
}

export async function bootstrap(): Promise<void> {
  state.booting = true;
  state.bootError = "";
  try {
    const me = await api.me();
    applySession(me.user, me.csrfToken, me.generation);
    state.sessionExpiredNotice = "";
    await loadInitialWorkspace();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      state.session = null;
      setCsrfToken(null);
    } else {
      state.bootError = describeError(error);
    }
  } finally {
    state.booting = false;
  }
}

export async function login(username: string, password: string): Promise<boolean> {
  state.loggingIn = true;
  state.loginError = "";
  try {
    const me = await api.login(username, password);
    applySession(me.user, me.csrfToken, me.generation);
    state.sessionExpiredNotice = "";
    state.logoutError = "";
    await loadInitialWorkspace();
    return true;
  } catch (error) {
    state.loginError = describeError(error);
    return false;
  } finally {
    state.loggingIn = false;
  }
}

export async function logout(): Promise<void> {
  state.loggingOut = true;
  state.logoutError = "";
  try {
    await api.logout();
  } catch (error) {
    // 服务端会话可能仍然有效；此时不假装已注销，保留会话并提示重试。
    state.logoutError = describeError(error);
    state.loggingOut = false;
    return;
  }
  state.loggingOut = false;
  state.sessionExpiredNotice = "";
  resetSession();
}

// --- 知识库与资料 ---------------------------------------------------------------

export async function loadKnowledgeBases(): Promise<void> {
  state.knowledgeBasesLoading = true;
  state.knowledgeBasesError = "";
  try {
    const response = await api.listKnowledgeBases();
    state.knowledgeBases = response.knowledgeBases;
  } catch (error) {
    state.knowledgeBasesError = describeError(error);
  } finally {
    state.knowledgeBasesLoading = false;
  }
}

export async function createKnowledgeBase(name: string): Promise<boolean> {
  const trimmed = name.trim();
  if (trimmed === "" || state.createKnowledgeBasePending) return false;
  state.createKnowledgeBasePending = true;
  state.createKnowledgeBaseError = "";
  try {
    const created = await api.createKnowledgeBase(trimmed);
    await loadKnowledgeBases();
    selectKnowledgeBase(created.id);
    return true;
  } catch (error) {
    state.createKnowledgeBaseError = describeError(error);
    return false;
  } finally {
    state.createKnowledgeBasePending = false;
  }
}

export function selectKnowledgeBase(kbId: string): void {
  if (kbId === state.activeKbId) return;
  state.activeKbId = kbId;
  state.documents = [];
  state.documentsError = "";
  documentsToken += 1;
  resetConversationSelection();
  void loadDocuments(kbId);
}

export async function loadDocuments(kbId: string, quiet = false): Promise<void> {
  if (kbId === "") return;
  const token = ++documentsToken;
  if (!quiet) {
    state.documentsLoading = true;
    state.documentsError = "";
  }
  try {
    const response = await api.listDocuments(kbId);
    if (token !== documentsToken || state.activeKbId !== kbId) return;
    state.documents = response.documents;
    state.documentsError = "";
  } catch (error) {
    if (token !== documentsToken || state.activeKbId !== kbId) return;
    // 静默轮询失败不覆盖已有列表，也不改写错误横幅；下一次轮询可自行恢复。
    if (!quiet || state.documents.length === 0) state.documentsError = describeError(error);
  } finally {
    if (token === documentsToken && state.activeKbId === kbId) {
      state.documentsLoading = false;
      if (state.documentsError === "") scheduleDocumentsPolling();
      else stopDocumentsPolling();
    }
  }
}

export async function refreshDocuments(): Promise<void> {
  await loadDocuments(state.activeKbId);
}

// --- 会话与消息 -----------------------------------------------------------------

export function conversationsForActiveKb(): ConversationSummary[] {
  if (state.activeKbId === "") return [];
  return state.conversations.filter((conversation) => conversation.kbIds.includes(state.activeKbId));
}

export async function loadConversations(quiet = false): Promise<void> {
  if (!quiet) {
    state.conversationsLoading = true;
    state.conversationsError = "";
  }
  try {
    const response = await api.listConversations();
    state.conversations = response.conversations;
    state.conversationsError = "";
  } catch (error) {
    // 静默刷新失败保留旧列表，避免一次瞬时错误清空界面。
    if (!quiet || state.conversations.length === 0) state.conversationsError = describeError(error);
  } finally {
    state.conversationsLoading = false;
  }
}

export async function startConversation(): Promise<boolean> {
  if (state.activeKbId === "" || state.newConversationPending) return false;
  state.newConversationPending = true;
  state.newConversationError = "";
  try {
    const created = await api.createConversation([state.activeKbId]);
    resetConversationSelection();
    state.activeConversationId = created.conversationId;
    void loadConversations(true);
    return true;
  } catch (error) {
    state.newConversationError = describeError(error);
    return false;
  } finally {
    state.newConversationPending = false;
  }
}

/** 用服务端返回的摘要替换本地列表项，并静默刷新以恢复置顶排序。 */
function mergeConversationSummary(updated: ConversationSummary): void {
  const index = state.conversations.findIndex((item) => item.id === updated.id);
  if (index >= 0) state.conversations[index] = updated;
  else state.conversations.push(updated);
  void loadConversations(true);
}

/** 改名：由服务端持久化标题；成功后就地更新列表并刷新排序。 */
export async function renameConversation(
  conversationId: string,
  title: string,
): Promise<boolean> {
  const trimmed = title.trim();
  state.conversationActionError = "";
  if (trimmed === "") {
    state.conversationActionError = "标题不能为空";
    return false;
  }
  try {
    const updated = await api.updateConversation(conversationId, { title: trimmed });
    mergeConversationSummary(updated);
    return true;
  } catch (error) {
    state.conversationActionError = describeError(error);
    return false;
  }
}

/** 置顶/取消置顶：`pinned` 是显式布尔，服务端负责写入或清空 `pinned_at`。 */
export async function setConversationPinned(
  conversationId: string,
  pinned: boolean,
): Promise<boolean> {
  state.conversationActionError = "";
  try {
    const updated = await api.updateConversation(conversationId, { pinned });
    mergeConversationSummary(updated);
    return true;
  } catch (error) {
    state.conversationActionError = describeError(error);
    return false;
  }
}

/** 逻辑删除：服务端软删；删除当前会话时回到空欢迎态，可继续新建会话。 */
export async function removeConversation(conversationId: string): Promise<boolean> {
  state.conversationActionError = "";
  try {
    await api.deleteConversation(conversationId);
    state.conversations = state.conversations.filter((item) => item.id !== conversationId);
    if (state.activeConversationId === conversationId) resetConversationSelection();
    return true;
  } catch (error) {
    state.conversationActionError = describeError(error);
    return false;
  }
}

export async function openConversation(conversationId: string): Promise<void> {
  citationToken += 1;
  state.citation = null;
  state.citationLoading = false;
  state.citationError = "";
  state.activeConversationId = conversationId;
  state.messages = [];
  state.messagesError = "";
  state.askError = "";
  state.lastFollowUp = "";
  await loadMessages(conversationId);
}

export async function loadMessages(conversationId: string, quiet = false): Promise<boolean> {
  const token = ++messagesToken;
  if (!quiet) {
    state.messagesLoading = true;
    state.messagesError = "";
  }
  let loaded = false;
  try {
    const response = await api.listMessages(conversationId);
    if (token !== messagesToken || state.activeConversationId !== conversationId) return false;
    state.messages = response.messages;
    state.messagesError = "";
    loaded = true;
  } catch (error) {
    if (token !== messagesToken || state.activeConversationId !== conversationId) return false;
    // 追问后的静默刷新失败时保留旧历史；失败提示由 askError 负责。
    if (!quiet || state.messages.length === 0) state.messagesError = describeError(error);
  } finally {
    if (token === messagesToken && state.activeConversationId === conversationId) {
      state.messagesLoading = false;
    }
  }
  return loaded;
}

/** 资料变化后刷新当前会话历史：服务端会隐藏已撤权或已删除来源的助手消息。 */
export async function refreshMessages(): Promise<void> {
  if (state.activeConversationId === "") return;
  await loadMessages(state.activeConversationId, true);
}

export async function ask(question: string): Promise<boolean> {
  const conversationId = state.activeConversationId;
  if (conversationId === "" || state.asking || state.session === null) return false;
  if (state.generation === null || state.generationModel === "") {
    state.askError = "生成能力尚未从服务端读到，无法提交本次提问";
    return false;
  }
  state.asking = true;
  state.askError = "";
  try {
    // 思考强度只在开启时随请求提交；关闭时服务端默认组合就是非思考。
    const options: AskGenerationOptions = {
      model: state.generationModel,
      thinking: state.generationThinking,
      ...(state.generationThinking === "enabled"
        ? { reasoningEffort: state.generationEffort }
        : {}),
    };
    // requestId 只用于服务端关联本次调用；前端不按它去重，也不自动重试付费消息。
    const answer = await api.ask(conversationId, question, crypto.randomUUID(), options);
    state.lastFollowUp = answer.followUp ?? "";
    const refreshed = await loadMessages(conversationId, true);
    if (!refreshed) {
      state.askError = "回答已生成，但历史刷新失败；请重新打开该会话查看结果。";
    }
    void loadConversations(true);
    return true;
  } catch (error) {
    state.askError = describeError(error);
    return false;
  } finally {
    state.asking = false;
  }
}

// --- 引用 ---------------------------------------------------------------------

export async function openCitation(citationId: string): Promise<void> {
  const token = ++citationToken;
  state.citation = null;
  state.citationError = "";
  state.citationLoading = true;
  try {
    // 每次点击都重新向后端取引用：权限与来源存在性由服务端复核。
    const citation = await api.getCitation(citationId);
    if (token !== citationToken) return;
    state.citation = citation;
  } catch (error) {
    if (token !== citationToken) return;
    state.citationError = describeError(error);
  } finally {
    if (token === citationToken) state.citationLoading = false;
  }
}

export function closeCitation(): void {
  citationToken += 1;
  state.citation = null;
  state.citationLoading = false;
  state.citationError = "";
}

export function activeKnowledgeBase(): KnowledgeBaseSummary | null {
  return state.knowledgeBases.find((kb) => kb.id === state.activeKbId) ?? null;
}

/** 工作台卸载时清理轮询定时器；注销路径已由 `resetSession` 处理。 */
export function disposeWorkspace(): void {
  stopDocumentsPolling();
}
