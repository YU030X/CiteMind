<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { PanelLeftIcon } from "@lucide/vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import CitationPanel from "@/components/CitationPanel.vue";
import ConversationList from "@/components/ConversationList.vue";
import KnowledgeBaseSelect from "@/components/KnowledgeBaseSelect.vue";
import { kbRoleLabel, formatTime } from "@/labels";
import { renderMarkdown } from "@/lib/markdown";
import {
  activeKnowledgeBase,
  ask,
  closeCitation,
  conversationsForActiveKb,
  openCitation,
  openConversation,
  startConversation,
  state,
} from "@/state/store";

const draft = ref("");
const historyOpen = ref(false);
const citationOpen = ref(false);
const scroller = ref<InstanceType<typeof ScrollArea> | null>(null);

/**
 * 生成侧能力：模型与 thinking 都固定在服务端（受限客户端显式 `thinking: {"type": "disabled"}`），
 * `GET /me`/登录响应只读返回实际模型与 thinking 关闭；`POST /conversations/{id}/messages` 的请求体
 * 只有 `question` 与 `requestId`，不接受模型或思考参数。两个控件只展示服务端实际值并始终禁用，
 * 不向服务端传值，也不假装可切换。
 */
const FIXED_THINKING_LABEL = "关闭";
const generationModel = computed(() => state.generation?.model ?? "—");
const capabilityHint = computed(() => {
  const generation = state.generation;
  if (generation === null) return "生成能力尚未从服务端读到，控件只读展示。";
  const disabled = generation.enabled ? "" : "；当前服务端未开启生成，提问会返回 503";
  return `回答由服务端生成：模型 ${generation.model}、thinking 固定关闭，前端只读展示、不可切换${disabled}。`;
});

const conversations = computed(() => conversationsForActiveKb());
const activeConversation = computed(
  () => state.conversations.find((item) => item.id === state.activeConversationId) ?? null,
);
const messages = computed(() => state.messages);
const hasMessages = computed(() => messages.value.length > 0);
const currentKb = computed(() => activeKnowledgeBase());
const canSend = computed(
  () =>
    state.activeConversationId !== "" &&
    draft.value.trim() !== "" &&
    !state.asking &&
    state.session !== null,
);

/** 会话固化的 KB 范围；已撤权或不可见的知识库只显示说明，不回退成 ID。 */
const scopeLabel = computed(() => {
  const ids = activeConversation.value?.kbIds ?? [];
  if (ids.length === 0) return "—";
  return ids
    .map((id) => state.knowledgeBases.find((kb) => kb.id === id)?.name ?? "已撤权或不可见的知识库")
    .join("、");
});

const title = computed(() => {
  const conversation = activeConversation.value;
  if (conversation === null) return "新的会话";
  const value = conversation.title?.trim() ?? "";
  return value === "" ? "未命名会话" : value;
});

const lastAssistantMessageId = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index]!;
    if (message.role === "assistant") return message.messageId;
  }
  return "";
});

const hint = computed(() => {
  if (state.asking) return "正在生成回答…（本次提交期间不能重复提交）";
  if (state.activeKbId === "") return "先选择一个知识库，再新建会话提问。";
  if (state.activeConversationId === "") return "在左侧选择会话，或点“新建会话”开始提问。";
  return capabilityHint.value;
});

/** 助手回答按 Markdown 渲染（`html: false`，原文 HTML 只按文本转义），一次算好避免重复解析。 */
const renderedMessages = computed(
  () =>
    new Map(
      messages.value.map((message) => [
        message.messageId,
        message.role === "assistant" ? renderMarkdown(message.content) : "",
      ]),
    ),
);

const citationVisible = computed(
  () => state.citation !== null || state.citationLoading || state.citationError !== "",
);

watch(citationVisible, (visible) => {
  citationOpen.value = visible;
});

watch(citationOpen, (open) => {
  if (!open) closeCitation();
});

watch([() => state.activeConversationId, () => messages.value.length], () => {
  void nextTick(scrollToBottom);
});

onBeforeUnmount(() => {
  closeCitation();
});

/** ScrollArea 的滚动视口在组件内部，按 data 属性取到后贴到底部。 */
function scrollToBottom(): void {
  const viewport = (scroller.value?.$el as HTMLElement | undefined)?.querySelector<HTMLElement>(
    '[data-slot="scroll-area-viewport"]',
  );
  if (viewport !== undefined && viewport !== null) viewport.scrollTop = viewport.scrollHeight;
}

async function send(): Promise<void> {
  const question = draft.value.trim();
  if (question === "" || !canSend.value) return;
  const ok = await ask(question);
  // 成功才清空草稿；失败时保留问题并只提示，不自动重试付费消息。
  if (ok) draft.value = "";
}

function selectConversation(conversationId: string): void {
  historyOpen.value = false;
  void openConversation(conversationId);
}

async function createConversation(): Promise<void> {
  historyOpen.value = false;
  await startConversation();
}

function applyFollowUp(text: string): void {
  draft.value = text;
}

function cite(citationId: string): void {
  void openCitation(citationId);
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col lg:flex-row">
    <!-- 会话历史：问答页自带的固定侧栏，不再是卡片 -->
    <aside class="hidden min-h-0 w-72 shrink-0 flex-col border-r bg-card xl:flex">
      <ConversationList
        class="p-3"
        :conversations="conversations"
        :active-id="state.activeConversationId"
        @select="selectConversation"
        @create="createConversation"
      />
    </aside>

    <section class="flex min-h-0 flex-1 flex-col bg-card">
      <header class="flex h-14 shrink-0 items-center gap-2 border-b px-3 lg:px-4">
        <Button
          variant="ghost"
          size="icon-sm"
          type="button"
          class="xl:hidden"
          aria-label="打开会话历史"
          aria-controls="chat-history"
          :aria-expanded="historyOpen"
          @click="historyOpen = true"
        >
          <PanelLeftIcon />
        </Button>

        <div class="flex min-w-0 flex-col">
          <p class="truncate text-sm font-medium">{{ title }}</p>
          <p class="truncate text-xs text-muted-foreground">知识库范围：{{ scopeLabel }}</p>
        </div>

        <div class="ml-auto flex shrink-0 items-center gap-2">
          <Badge v-if="currentKb !== null" variant="outline">
            {{ kbRoleLabel(currentKb.role) }}
          </Badge>
          <KnowledgeBaseSelect />
        </div>
      </header>

      <!-- 有消息：消息流自己滚动，输入框留在主内容底部 -->
      <ScrollArea v-if="hasMessages" ref="scroller" class="min-h-0 flex-1">
        <div class="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
          <template v-for="message in messages" :key="message.messageId">
            <!-- 用户：浅灰小气泡，右对齐 -->
            <div v-if="message.role === 'user'" class="flex justify-end">
              <p
                class="max-w-[80%] rounded-md bg-secondary px-3 py-2 text-sm leading-6 whitespace-pre-wrap"
              >
                {{ message.content }}
              </p>
            </div>

            <!-- 助手：无气泡正文 -->
            <div v-else class="flex flex-col gap-2">
              <div class="flex flex-wrap items-center gap-2">
                <span
                  aria-hidden="true"
                  class="grid size-6 shrink-0 place-items-center rounded-md border bg-card text-xs font-medium"
                >
                  知
                </span>
                <span class="text-xs text-muted-foreground">
                  知据助手 · {{ formatTime(message.createdAt) }}
                </span>
              </div>

              <div class="md-body text-sm leading-7" v-html="renderedMessages.get(message.messageId)" />

              <div v-if="message.citations.length > 0" class="flex flex-wrap gap-2">
                <button
                  v-for="citation in message.citations"
                  :key="citation.citationId"
                  type="button"
                  class="citation-chip"
                  :title="`查看引用：${citation.documentTitle} v${citation.version}`"
                  @click="cite(citation.citationId)"
                >
                  {{ citation.displayLabel }}
                </button>
              </div>

              <div
                v-if="
                  message.messageId === lastAssistantMessageId &&
                  state.lastFollowUp !== '' &&
                  !state.asking
                "
                class="flex flex-wrap gap-2"
              >
                <button
                  type="button"
                  class="rounded-md border border-dashed px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
                  @click="applyFollowUp(state.lastFollowUp)"
                >
                  服务端建议的追问：{{ state.lastFollowUp }}
                </button>
              </div>
            </div>
          </template>

          <p v-if="state.asking" class="text-sm text-muted-foreground">
            正在生成回答…（本次提交期间不能重复提交）
          </p>
        </div>
      </ScrollArea>

      <!--
        输入区：空会话时与新会话欢迎标题一起垂直居中，有消息时贴在主内容底部。
        两种状态是同一个输入框、同一份状态，交互完全一致。
      -->
      <div
        class="shrink-0"
        :class="
          hasMessages
            ? 'px-4 py-3'
            : 'flex min-h-0 flex-1 flex-col items-center justify-center gap-6 overflow-y-auto px-4 py-6'
        "
      >
        <div v-if="!hasMessages" class="flex flex-col items-center gap-3 text-center">
          <h1 class="text-xl font-medium">问知据助手</h1>
          <p class="max-w-md text-sm text-muted-foreground">
            回答基于当前知识库里你有权访问的资料，回答下方的引用可以点开核对原文与版本。
          </p>
          <Skeleton v-if="state.messagesLoading" class="h-4 w-40" />
          <Alert v-else-if="state.messagesError !== ''" variant="destructive" class="text-left">
            <AlertTitle>无法载入历史</AlertTitle>
            <AlertDescription>{{ state.messagesError }}</AlertDescription>
          </Alert>
          <p v-else-if="state.activeConversationId === ''" class="text-sm text-muted-foreground">
            还没有打开会话。
          </p>
          <p v-else class="text-sm text-muted-foreground">
            这个会话还没有消息，输入问题开始提问。
          </p>
        </div>

        <div class="mx-auto w-full" :class="hasMessages ? 'max-w-3xl' : 'max-w-2xl'">
          <div
            class="rounded-md border bg-card p-2 transition-[color,box-shadow] focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50"
          >
            <Textarea
              id="chat-input"
              v-model="draft"
              class="resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
              :class="hasMessages ? 'max-h-40 min-h-20' : 'max-h-40 min-h-32'"
              maxlength="8000"
              aria-label="输入问题"
              placeholder="就当前知识库里的资料提问（Enter 发送，Shift+Enter 换行）"
              :disabled="state.asking || state.activeConversationId === ''"
              @keydown.enter.exact.prevent="send"
            />

            <!-- 底部工具条：左侧是当前状态说明，右侧是固定合同的两个控件与发送。 -->
            <div class="mt-1 flex flex-wrap items-center gap-2 px-1">
              <p class="hidden min-w-0 flex-1 truncate text-xs text-muted-foreground sm:block">
                {{ hint }}
              </p>

              <div class="ml-auto flex shrink-0 items-center gap-1.5">
                <Select :model-value="generationModel" disabled>
                  <SelectTrigger
                    size="sm"
                    class="w-36"
                    aria-label="模型（服务端固定，不可切换）"
                    :title="capabilityHint"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem :value="generationModel">{{ generationModel }}</SelectItem>
                  </SelectContent>
                </Select>

                <Select :model-value="FIXED_THINKING_LABEL" disabled>
                  <SelectTrigger
                    size="sm"
                    class="w-20"
                    aria-label="思考（服务端固定关闭，不可切换）"
                    :title="capabilityHint"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem :value="FIXED_THINKING_LABEL">{{ FIXED_THINKING_LABEL }}</SelectItem>
                  </SelectContent>
                </Select>

                <Button
                  type="button"
                  size="sm"
                  class="shrink-0 hover:bg-primary-hover"
                  :disabled="!canSend"
                  @click="send"
                >
                  {{ state.asking ? "生成中…" : "发送" }}
                </Button>
              </div>
            </div>
          </div>

          <Alert v-if="state.askError !== ''" variant="destructive" class="mt-2">
            <AlertTitle>提问失败</AlertTitle>
            <AlertDescription>
              {{ state.askError }}。本次没有自动重试；需要重试时请确认后再点“发送”。
            </AlertDescription>
          </Alert>
        </div>
      </div>
    </section>

    <!-- 会话历史：窄屏抽屉，避免与全局导航争抢宽度 -->
    <Sheet v-model:open="historyOpen">
      <SheetContent id="chat-history" side="left" class="w-72">
        <SheetHeader>
          <SheetTitle>会话历史</SheetTitle>
          <SheetDescription>只显示包含当前知识库的会话。</SheetDescription>
        </SheetHeader>
        <ConversationList
          class="min-h-0 flex-1 px-4 pb-4"
          :conversations="conversations"
          :active-id="state.activeConversationId"
          @select="selectConversation"
          @create="createConversation"
        />
      </SheetContent>
    </Sheet>

    <!-- 引用详情：始终用右侧 Sheet 按需弹出，关闭后问答区恢复完整宽度 -->
    <Sheet v-model:open="citationOpen">
      <SheetContent side="right" class="sm:max-w-md!">
        <SheetHeader>
          <SheetTitle>引用详情</SheetTitle>
          <SheetDescription>每次打开都重新向服务端复核权限与来源。</SheetDescription>
        </SheetHeader>
        <div class="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
          <CitationPanel />
        </div>
      </SheetContent>
    </Sheet>
  </div>
</template>

<style scoped>
/*
 * shadcn 没有引入 typography 插件，这里只补齐问答正文需要的少量排版。
 * 正文是 v-html 注入的，样式必须走 :deep()。
 */
.md-body :deep(p) {
  margin: 0;
}

.md-body :deep(p + p) {
  margin-top: 0.75rem;
}

.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4) {
  margin: 0.75rem 0 0;
  font-weight: 600;
}

.md-body :deep(ul),
.md-body :deep(ol) {
  margin: 0.5rem 0 0;
  padding-left: 1.25rem;
}

.md-body :deep(ul) {
  list-style: disc;
}

.md-body :deep(ol) {
  list-style: decimal;
}

.md-body :deep(li) {
  margin-top: 0.25rem;
}

.md-body :deep(li::marker) {
  color: var(--muted-foreground);
}

.md-body :deep(strong) {
  font-weight: 600;
}

.md-body :deep(a) {
  color: var(--primary);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.md-body :deep(code) {
  border-radius: 4px;
  background: var(--secondary);
  padding: 0.1rem 0.3rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.8125rem;
}

.md-body :deep(pre) {
  margin: 0.75rem 0 0;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--secondary);
  padding: 0.75rem;
}

.md-body :deep(pre code) {
  background: transparent;
  padding: 0;
  font-size: 0.8125rem;
  line-height: 1.6;
}

.md-body :deep(blockquote) {
  margin: 0.75rem 0 0;
  border-left: 2px solid var(--border);
  padding-left: 0.75rem;
  color: var(--muted-foreground);
}

.md-body :deep(table) {
  margin-top: 0.75rem;
  border-collapse: collapse;
}

.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid var(--border);
  padding: 0.25rem 0.5rem;
}

.citation-chip {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--card);
  padding: 0 0.375rem;
  color: var(--primary);
  font-size: 0.75rem;
  font-weight: 500;
  line-height: 1.5rem;
  cursor: pointer;
  transition: background-color 0.15s;
}

.citation-chip:hover {
  background: var(--secondary);
}

.citation-chip:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 1px;
}
</style>
