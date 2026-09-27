<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { PanelLeftIcon } from "@lucide/vue";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
import { Textarea } from "@/components/ui/textarea";
import { renderMarkdown } from "@/lib/markdown";
import CitationPanel from "./CitationPanel.vue";
import ConversationList from "./ConversationList.vue";
import {
  citations,
  conversationMessages,
  conversations,
  demoModels,
  pickSimulatedAnswer,
  thinkingLevels,
  type ChatMessage,
  type Citation,
  type Conversation,
  type ThinkingLevel,
} from "./data";

const citationById = new Map(citations.map((citation) => [citation.id, citation]));

const items = ref<Conversation[]>(conversations.map((conversation) => ({ ...conversation })));
const threads = ref<Record<string, ChatMessage[]>>(
  Object.fromEntries(
    Object.entries(conversationMessages).map(([id, thread]) => [
      id,
      thread.map((message) => ({ ...message })),
    ]),
  ),
);
const activeId = ref(conversations[0]?.id ?? "");
const draft = ref("");
/** 输入区选择项：只在本地生效，不参与模拟回答的内容。 */
const selectedModel = ref(demoModels[0]?.value ?? "deepseek-flash");
const thinking = ref<ThinkingLevel>("off");
/** 本地模拟回答生成中，用来阻止重复发送。 */
const pending = ref(false);
/** 正在等待回答的会话 ID，用来在会话被删除后阻止定时器回写。 */
const pendingId = ref<string | null>(null);
/** 待确认删除的会话；非空时弹出确认弹窗。 */
const deleteTarget = ref<Conversation | null>(null);
const deleteOpen = ref(false);
/** 正在改名的会话与预填标题；非空时弹出标题编辑弹窗。 */
const renameTarget = ref<Conversation | null>(null);
const renameOpen = ref(false);
const renameDraft = ref("");
const activeCitation = ref<Citation | null>(null);
const citationOpen = ref(false);
const historyOpen = ref(false);
const notice = ref("演示环境：回答由本地模拟生成，未调用后端接口。");
const scroller = ref<InstanceType<typeof ScrollArea> | null>(null);
let replyTimer: number | undefined;

const activeConversation = computed(
  () => items.value.find((conversation) => conversation.id === activeId.value) ?? null,
);
const messages = computed(() => threads.value[activeId.value] ?? []);
const hasMessages = computed(() => messages.value.length > 0);
// 空列表（会话全被删除）时没有可发送的目标，按钮同步禁用而不是静默无响应。
const canSend = computed(
  () => draft.value.trim() !== "" && !pending.value && activeConversation.value !== null,
);
/** 标题去掉首尾空格后不能为空；为空时禁用保存，取消则不改动。 */
const renameValid = computed(() => renameDraft.value.trim() !== "");
const hint = computed(() => (pending.value ? "正在生成本地模拟回答…" : notice.value));
const modelLabel = computed(
  () =>
    demoModels.find((option) => option.value === selectedModel.value)?.label ??
    selectedModel.value,
);
const thinkingLabel = computed(
  () => thinkingLevels.find((level) => level.value === thinking.value)?.label ?? "关闭",
);
/** 先渲染好每条助手回答，避免在输入框打字时反复重解析 Markdown。 */
const renderedMessages = computed(
  () =>
    new Map(
      messages.value.map((message) => [
        message.id,
        message.role === "assistant"
          ? renderMarkdown(message.content, citationMarkers(message))
          : "",
      ]),
    ),
);

watch([activeId, () => messages.value.length], () => {
  void nextTick(scrollToBottom);
});

onBeforeUnmount(() => {
  if (replyTimer !== undefined) window.clearTimeout(replyTimer);
});

/** ScrollArea 的滚动视口在组件内部，按 data 属性取到后贴到底部。 */
function scrollToBottom() {
  const viewport = (scroller.value?.$el as HTMLElement | undefined)?.querySelector<HTMLElement>(
    '[data-slot="scroll-area-viewport"]',
  );
  if (viewport !== undefined && viewport !== null) viewport.scrollTop = viewport.scrollHeight;
}

/**
 * 只把本条回答引用的、且本地引用映射里确实存在的引用开放成可点击标记。
 * 正文里的 `[1]` 因此无法凭自己造出引用入口。
 */
function citationMarkers(message: ChatMessage): ReadonlyMap<string, string> {
  const markers = new Map<string, string>();
  for (const id of message.citations ?? []) {
    const citation = citationById.get(id);
    if (citation !== undefined) markers.set(citation.marker, citation.id);
  }
  return markers;
}

function openCitation(id: string) {
  const citation = citationById.get(id);
  if (citation === undefined) return;
  activeCitation.value = citation;
  citationOpen.value = true;
}

/** 引用按钮由本地渲染生成，这里只读取 data 属性里的 ID，不执行正文里的任何标签。 */
function onMessageClick(event: MouseEvent) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const id = target.closest("[data-citation-id]")?.getAttribute("data-citation-id");
  if (id !== null && id !== undefined) openCitation(id);
}

function selectConversation(id: string) {
  activeId.value = id;
  historyOpen.value = false;
}

/** 只记录待删除的会话，真正的本地删除在确认后执行。 */
function requestDelete(id: string) {
  const conversation = items.value.find((item) => item.id === id) ?? null;
  if (conversation === null) return;
  deleteTarget.value = conversation;
  deleteOpen.value = true;
}

/** 打开标题编辑：预填当前标题，取消不修改。 */
function requestRename(id: string) {
  const conversation = items.value.find((item) => item.id === id) ?? null;
  if (conversation === null) return;
  renameTarget.value = conversation;
  renameDraft.value = conversation.title;
  renameOpen.value = true;
}

/** 保存标题：空标题不提交；只改本地列表，不动消息线程。 */
function confirmRename() {
  const target = renameTarget.value;
  const title = renameDraft.value.trim();
  if (target === null || title === "") return;
  items.value = items.value.map((item) =>
    item.id === target.id ? { ...item, title } : item,
  );
  renameOpen.value = false;
  renameTarget.value = null;
  notice.value = "演示：会话标题只在本地列表更新，未调用后端接口。";
}

/** 置顶/取消置顶只切换本地标记，切换会话与新建对话都不会重置。 */
function togglePin(id: string) {
  const target = items.value.find((item) => item.id === id);
  if (target === undefined) return;
  const nextPinned = target.pinned !== true;
  items.value = items.value.map((item) =>
    item.id === id ? { ...item, pinned: nextPinned } : item,
  );
  notice.value = nextPinned
    ? "演示：会话已置顶到本地列表，未调用后端接口。"
    : "演示：会话已取消置顶，未调用后端接口。";
}

function clearReplyTimer() {
  if (replyTimer !== undefined) window.clearTimeout(replyTimer);
  replyTimer = undefined;
  pending.value = false;
  pendingId.value = null;
}

function confirmDelete() {
  const target = deleteTarget.value;
  deleteOpen.value = false;
  deleteTarget.value = null;
  if (target === null) return;

  // 删掉还在等待回答的会话时，先停掉定时器，避免回答回来把会话重建出来。
  if (pendingId.value === target.id) clearReplyTimer();

  items.value = items.value.filter((item) => item.id !== target.id);
  threads.value = Object.fromEntries(
    Object.entries(threads.value).filter(([id]) => id !== target.id),
  );

  // 当前会话被删就退到列表里的第一个会话；列表空了则回到空欢迎态。
  if (activeId.value === target.id) activeId.value = items.value[0]?.id ?? "";
  notice.value = "演示：会话已从本地列表移除，未调用后端接口。";
}

function createConversation() {
  const id = `demo-conv-${Date.now()}`;
  items.value = [
    {
      id,
      title: "新的对话",
      scope: "员工手册",
      group: "今天",
      updatedAt: "刚刚",
    },
    ...items.value,
  ];
  threads.value = { ...threads.value, [id]: [] };
  activeId.value = id;
  draft.value = "";
  historyOpen.value = false;
  notice.value = "演示：新对话只加在本地列表里，未调用后端接口。";
}

function sendMessage() {
  const text = draft.value.trim();
  const conversation = activeConversation.value;
  if (text === "" || conversation === null || pending.value) return;

  const id = activeId.value;
  const thread = threads.value[id] ?? [];

  // 空对话的第一条提问同时作为本地标题，方便在历史里区分。
  if (thread.length === 0) {
    items.value = items.value.map((item) =>
      item.id === id ? { ...item, title: text.slice(0, 18) } : item,
    );
  }

  threads.value = {
    ...threads.value,
    [id]: [...thread, { id: `demo-user-${Date.now()}`, role: "user", content: text }],
  };
  draft.value = "";
  pending.value = true;
  pendingId.value = id;
  notice.value = "演示：消息只加在本地列表里，未发送到任何服务。";

  const answer = pickSimulatedAnswer(text);
  // 把当下的选择快照写进这条回答，之后切换模型不会改写已有历史。
  const snapshot = { modelLabel: modelLabel.value, thinkingLabel: thinkingLabel.value };
  replyTimer = window.setTimeout(() => {
    replyTimer = undefined;
    pending.value = false;
    pendingId.value = null;
    // 会话在等待期间被删除时不再回写，避免复活已删除的会话。
    if (!items.value.some((item) => item.id === id)) return;
    threads.value = {
      ...threads.value,
      [id]: [
        ...(threads.value[id] ?? []),
        {
          id: `demo-assistant-${Date.now()}`,
          role: "assistant",
          content: answer.content,
          citations: answer.citations,
          ...snapshot,
        },
      ],
    };
  }, 600);
}

function applyFollowUp(text: string) {
  draft.value = text;
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col lg:flex-row">
    <!-- 会话历史：问答页自带的固定侧栏，不再是卡片 -->
    <aside class="hidden min-h-0 w-72 shrink-0 flex-col border-r bg-card xl:flex">
      <ConversationList
        class="p-3"
        :conversations="items"
        :active-id="activeId"
        @select="selectConversation"
        @create="createConversation"
        @request-delete="requestDelete"
        @request-rename="requestRename"
        @toggle-pin="togglePin"
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
          aria-controls="demo-chat-history"
          :aria-expanded="historyOpen"
          @click="historyOpen = true"
        >
          <PanelLeftIcon />
        </Button>

        <div class="flex min-w-0 flex-col">
          <p class="truncate text-sm font-medium">
            {{ activeConversation?.title ?? "新的对话" }}
          </p>
          <p class="truncate text-xs text-muted-foreground">
            知识库范围：{{ activeConversation?.scope ?? "—" }}
          </p>
        </div>

        <Badge variant="outline" class="ml-auto shrink-0">本地模拟</Badge>
      </header>

      <!-- 有消息：消息流自己滚动，输入框留在主内容底部 -->
      <ScrollArea v-if="hasMessages" ref="scroller" class="min-h-0 flex-1">
        <div class="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
          <template v-for="message in messages" :key="message.id">
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
              <div class="flex items-center gap-2">
                <span
                  aria-hidden="true"
                  class="grid size-6 shrink-0 place-items-center rounded-md border bg-card text-xs font-medium"
                >
                  C
                </span>
                <span class="text-xs text-muted-foreground">CiteMind 助手 · 本地模拟回答</span>
                <span v-if="message.modelLabel" class="truncate text-xs text-muted-foreground">
                  {{ message.modelLabel }} · 思考：{{ message.thinkingLabel }}
                </span>
              </div>

              <div
                class="md-body text-sm leading-7"
                v-html="renderedMessages.get(message.id)"
                @click="onMessageClick"
              />

              <div v-if="message.followUps?.length" class="flex flex-wrap gap-2">
                <button
                  v-for="followUp in message.followUps"
                  :key="followUp"
                  type="button"
                  class="rounded-md border border-dashed px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
                  @click="applyFollowUp(followUp)"
                >
                  {{ followUp }}
                </button>
              </div>
            </div>
          </template>
        </div>
      </ScrollArea>

      <!--
        输入区：空对话时与新会话欢迎标题一起垂直居中，有消息时贴在主内容底部。
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
        <div v-if="!hasMessages" class="text-center">
          <h1 class="text-xl font-medium">问 CiteMind 助手</h1>
          <p class="mt-2 text-sm text-muted-foreground">
            基于已入库的文档回答，正文里的 [1] 可以点开看引用原文。
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
              :class="hasMessages ? 'min-h-20 max-h-40' : 'min-h-32 max-h-40'"
              aria-label="输入问题"
              placeholder="输入问题…（演示环境，不会发送到任何服务）"
              @keydown.enter.exact.prevent="sendMessage"
            />

            <!--
              底部工具条：左侧是本页一直显示的演示说明，右侧是模型、思考程度与发送。
              窄屏隐藏说明并用 flex-wrap 兜底，保证控件不溢出。
            -->
            <div class="mt-1 flex flex-wrap items-center gap-2 px-1">
              <p class="hidden min-w-0 flex-1 truncate text-xs text-muted-foreground sm:block">
                {{ hint }}
              </p>

              <div class="ml-auto flex shrink-0 items-center gap-1.5">
                <!-- 两个选择器都是本地界面状态，不会改动模拟回答的内容，也没有真实推理参数。 -->
                <Select v-model="selectedModel" :disabled="pending">
                  <SelectTrigger
                    size="sm"
                    class="w-36"
                    aria-label="演示模型"
                    title="演示模型（仅本地界面状态）"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem
                      v-for="option in demoModels"
                      :key="option.value"
                      :value="option.value"
                    >
                      {{ option.label }}
                    </SelectItem>
                  </SelectContent>
                </Select>

                <Select v-model="thinking" :disabled="pending">
                  <SelectTrigger
                    size="sm"
                    class="w-20"
                    aria-label="思考程度"
                    title="思考程度（仅本地界面状态）"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem
                      v-for="level in thinkingLevels"
                      :key="level.value"
                      :value="level.value"
                    >
                      {{ level.label }}
                    </SelectItem>
                  </SelectContent>
                </Select>

                <Button
                  type="button"
                  size="sm"
                  class="shrink-0 hover:bg-primary-hover"
                  :disabled="!canSend"
                  @click="sendMessage"
                >
                  发送
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 会话历史：窄屏抽屉，避免与全局导航争抢宽度 -->
    <Sheet v-model:open="historyOpen">
      <SheetContent id="demo-chat-history" side="left">
        <SheetHeader>
          <SheetTitle>会话历史</SheetTitle>
          <SheetDescription>按时间分组，切换已有对话或新建。</SheetDescription>
        </SheetHeader>
        <ConversationList
          class="min-h-0 flex-1 px-4 pb-4"
          :conversations="items"
          :active-id="activeId"
          @select="selectConversation"
          @create="createConversation"
          @request-delete="requestDelete"
          @request-rename="requestRename"
          @toggle-pin="togglePin"
        />
      </SheetContent>
    </Sheet>

    <!-- 删除确认：只删本地列表状态，不影响任何服务端数据 -->
    <Dialog v-model:open="deleteOpen">
      <DialogContent class="sm:max-w-sm [--destructive:#dc2626]">
        <DialogHeader>
          <DialogTitle>删除会话</DialogTitle>
          <DialogDescription>
            将从本地演示列表移除「{{ deleteTarget?.title ?? "" }}」及其消息。只改本地状态，不会调用后端接口。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" type="button" @click="deleteOpen = false">取消</Button>
          <Button variant="destructive" type="button" @click="confirmDelete">删除</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- 修改标题：预填当前标题，空标题不可保存，取消不改动；只改本地列表 -->
    <Dialog v-model:open="renameOpen">
      <DialogContent class="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>修改会话标题</DialogTitle>
          <DialogDescription>
            只改本地演示列表里的标题，不会调用后端接口。
          </DialogDescription>
        </DialogHeader>
        <div class="flex flex-col gap-2">
          <Label for="rename-title">标题</Label>
          <Input
            id="rename-title"
            v-model="renameDraft"
            maxlength="40"
            placeholder="输入会话标题"
            @keydown.enter.prevent="confirmRename"
          />
        </div>
        <DialogFooter>
          <Button variant="outline" type="button" @click="renameOpen = false">取消</Button>
          <Button type="button" :disabled="!renameValid" @click="confirmRename">保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- 引用详情：始终用右侧 Sheet 按需弹出，关闭后问答区恢复完整宽度 -->
    <Sheet v-model:open="citationOpen">
      <SheetContent side="right" class="sm:max-w-md!">
        <SheetHeader>
          <SheetTitle>引用详情</SheetTitle>
          <SheetDescription>引用内容为静态样例，未从服务端读取。</SheetDescription>
        </SheetHeader>
        <div class="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
          <CitationPanel v-if="activeCitation" :citation="activeCitation" />
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

.md-body :deep(.md-citation) {
  display: inline-flex;
  align-items: center;
  margin: 0 0.1rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--card);
  padding: 0 0.25rem;
  color: var(--primary);
  font-size: 0.75rem;
  font-weight: 500;
  line-height: 1.25rem;
  cursor: pointer;
  transition: background-color 0.15s;
}

.md-body :deep(.md-citation:hover) {
  background: var(--secondary);
}

.md-body :deep(.md-citation:focus-visible) {
  outline: 2px solid var(--primary);
  outline-offset: 1px;
}
</style>
