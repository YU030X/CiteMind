<script setup lang="ts">
import { computed, ref } from "vue";
import {
  EllipsisIcon,
  PencilLineIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  Trash2Icon,
} from "@lucide/vue";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { ConversationSummary } from "@/api/types";
import { dayGroupLabel, formatTime } from "@/labels";
import {
  removeConversation,
  renameConversation,
  setConversationPinned,
  state,
} from "@/state/store";

const props = defineProps<{
  conversations: ConversationSummary[];
  activeId: string;
}>();

const emit = defineEmits<{
  select: [id: string];
  create: [];
}>();

/**
 * 时间分组展示顺序，空分组不占位。
 * 分组依据是最近消息时间；没有消息时回退到创建时间。置顶会话由服务端排在最前，这里不重排。
 */
const groups = ["今天", "昨天", "更早"] as const;

const sections = computed(() =>
  groups
    .map((label) => ({
      label,
      items: props.conversations.filter(
        (conversation) =>
          dayGroupLabel(conversation.lastMessageAt ?? conversation.createdAt) === label,
      ),
    }))
    .filter((section) => section.items.length > 0),
);

function conversationTime(conversation: ConversationSummary): string {
  return formatTime(conversation.lastMessageAt ?? conversation.createdAt);
}

/** 标题为空表示还没有首个问题，用中性占位而不是编造内容。 */
function conversationTitle(conversation: ConversationSummary): string {
  const title = conversation.title?.trim() ?? "";
  return title === "" ? "未命名会话" : title;
}

// --- 改名 ---------------------------------------------------------------------

const renameTarget = ref<ConversationSummary | null>(null);
const renameOpen = ref(false);
const renameTitle = ref("");
const renamePending = ref(false);

function openRename(conversation: ConversationSummary): void {
  renameTarget.value = conversation;
  renameTitle.value = conversation.title ?? "";
  state.conversationActionError = "";
  renameOpen.value = true;
}

async function submitRename(): Promise<void> {
  const target = renameTarget.value;
  if (target === null || renamePending.value) return;
  renamePending.value = true;
  try {
    const ok = await renameConversation(target.id, renameTitle.value);
    if (ok) {
      renameOpen.value = false;
      renameTarget.value = null;
    }
  } finally {
    renamePending.value = false;
  }
}

// --- 置顶 ---------------------------------------------------------------------

async function togglePin(conversation: ConversationSummary): Promise<void> {
  state.conversationActionError = "";
  await setConversationPinned(conversation.id, !conversation.pinned);
}

// --- 删除 ---------------------------------------------------------------------

const deleteTarget = ref<ConversationSummary | null>(null);
const deleteOpen = ref(false);
const deletePending = ref(false);

function requestDelete(conversation: ConversationSummary): void {
  deleteTarget.value = conversation;
  state.conversationActionError = "";
  deleteOpen.value = true;
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value;
  if (target === null || deletePending.value) return;
  deletePending.value = true;
  try {
    const ok = await removeConversation(target.id);
    if (ok) {
      deleteOpen.value = false;
      deleteTarget.value = null;
    }
  } finally {
    deletePending.value = false;
  }
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col gap-3">
    <Button variant="outline" class="w-full justify-start" type="button" @click="emit('create')">
      <PlusIcon />
      新建会话
    </Button>

    <p
      v-if="state.conversationActionError !== ''"
      class="rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1 text-xs text-destructive"
    >
      {{ state.conversationActionError }}
    </p>

    <div class="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1">
      <section v-for="section in sections" :key="section.label" class="flex flex-col gap-1">
        <h2 class="flex items-center gap-1 px-1 text-xs font-medium text-muted-foreground">
          {{ section.label }}
        </h2>

        <ul class="flex flex-col gap-0.5">
          <li
            v-for="conversation in section.items"
            :key="conversation.id"
            class="group flex items-center rounded-md transition-colors"
            :class="conversation.id === activeId ? 'bg-secondary' : 'hover:bg-secondary/70'"
          >
            <button
              type="button"
              class="min-w-0 flex-1 rounded-md px-2 py-1.5 text-left transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
              :class="
                conversation.id === activeId
                  ? 'text-foreground'
                  : 'text-muted-foreground group-hover:text-foreground'
              "
              :aria-current="conversation.id === activeId ? 'true' : undefined"
              @click="emit('select', conversation.id)"
            >
              <span class="flex items-center gap-1.5">
                <PinIcon
                  v-if="conversation.pinned"
                  class="size-3.5 shrink-0 text-primary"
                  aria-label="已置顶"
                />
                <span
                  class="line-clamp-1 text-sm"
                  :class="conversation.id === activeId ? 'font-medium' : undefined"
                >
                  {{ conversationTitle(conversation) }}
                </span>
              </span>
              <span class="mt-0.5 block text-xs text-muted-foreground">
                {{ conversationTime(conversation) }}
              </span>
            </button>

            <!--
              省略号菜单是行按钮的兄弟节点，点它不会触发行选中；
              `[--destructive]` 只在本菜单内把危险操作改成红色，不动全局 token。
              三项操作都调用真实的服务端持久化接口。
            -->
            <DropdownMenu>
              <DropdownMenuTrigger as-child>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  type="button"
                  class="shrink-0 text-muted-foreground hover:bg-transparent hover:text-foreground aria-expanded:bg-transparent"
                  :aria-label="`会话「${conversationTitle(conversation)}」的操作菜单`"
                >
                  <EllipsisIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" class="w-48 [--destructive:#dc2626]">
                <DropdownMenuItem @select="togglePin(conversation)">
                  <PinOffIcon v-if="conversation.pinned" />
                  <PinIcon v-else />
                  {{ conversation.pinned ? "取消置顶" : "置顶会话" }}
                </DropdownMenuItem>
                <DropdownMenuItem @select="openRename(conversation)">
                  <PencilLineIcon />
                  修改标题
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem variant="destructive" @select="requestDelete(conversation)">
                  <Trash2Icon />
                  删除会话
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </li>
        </ul>
      </section>
    </div>

    <Dialog v-model:open="renameOpen">
      <DialogContent class="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>修改标题</DialogTitle>
          <DialogDescription>标题由服务端持久化，最长 200 个字符。</DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-2">
          <Label for="conversation-title">标题</Label>
          <Input
            id="conversation-title"
            v-model="renameTitle"
            maxlength="200"
            placeholder="为此会话填写标题"
            :disabled="renamePending"
            @keydown.enter.exact.prevent="submitRename"
          />
        </div>

        <p v-if="state.conversationActionError !== ''" class="text-sm text-destructive">
          {{ state.conversationActionError }}
        </p>

        <DialogFooter>
          <Button variant="outline" type="button" @click="renameOpen = false">取消</Button>
          <Button
            type="button"
            class="hover:bg-primary-hover"
            :disabled="renamePending || renameTitle.trim() === ''"
            @click="submitRename"
          >
            {{ renamePending ? "保存中…" : "保存" }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="deleteOpen">
      <DialogContent class="sm:max-w-sm [--destructive:#dc2626]">
        <DialogHeader>
          <DialogTitle>删除会话</DialogTitle>
          <DialogDescription>
            将删除「{{ deleteTarget === null ? "" : conversationTitle(deleteTarget) }}」：
            该会话的历史与引用立即不可访问，操作不可撤销。
          </DialogDescription>
        </DialogHeader>

        <p v-if="state.conversationActionError !== ''" class="text-sm text-destructive">
          {{ state.conversationActionError }}
        </p>

        <DialogFooter>
          <Button variant="outline" type="button" @click="deleteOpen = false">取消</Button>
          <Button
            variant="destructive"
            type="button"
            :disabled="deletePending"
            @click="confirmDelete"
          >
            {{ deletePending ? "删除中…" : "确认删除" }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
