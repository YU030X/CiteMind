<script setup lang="ts">
import { computed } from "vue";
import { EllipsisIcon, PinIcon, PlusIcon } from "@lucide/vue";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Conversation, ConversationGroup } from "@/demos/data";

const props = defineProps<{
  conversations: Conversation[];
  activeId: string;
}>();

const emit = defineEmits<{
  select: [id: string];
  create: [];
  /** 只发起删除意图，本地删除与确认弹窗由问答页统一持有。 */
  requestDelete: [id: string];
  /** 只发起改名意图，标题编辑弹窗由问答页统一持有。 */
  requestRename: [id: string];
  /** 置顶/取消置顶都只改本地列表状态，由问答页持有。 */
  togglePin: [id: string];
}>();

/** 时间分组展示顺序，空分组不占位。 */
const groups: ConversationGroup[] = ["今天", "昨天", "更早"];

interface Section {
  key: string;
  label: string;
  pinned: boolean;
  items: Conversation[];
}

/**
 * 置顶项固定为最上面的独立分组，其余仍按今天/昨天/更早排列。
 * 组内顺序保持传入数组顺序；置顶与改名都走原地 map，排序稳定。
 */
const sections = computed<Section[]>(() => {
  const result: Section[] = [];
  const pinned = props.conversations.filter((conversation) => conversation.pinned === true);
  if (pinned.length > 0) {
    result.push({ key: "pinned", label: "置顶", pinned: true, items: pinned });
  }
  for (const label of groups) {
    const items = props.conversations.filter(
      (conversation) => conversation.pinned !== true && conversation.group === label,
    );
    if (items.length > 0) {
      result.push({ key: label, label, pinned: false, items });
    }
  }
  return result;
});
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col gap-3">
    <Button
      variant="outline"
      class="w-full justify-start"
      type="button"
      @click="emit('create')"
    >
      <PlusIcon />
      新建对话
    </Button>

    <div class="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1">
      <section v-for="section in sections" :key="section.key" class="flex flex-col gap-1">
        <h2 class="flex items-center gap-1 px-1 text-xs font-medium text-muted-foreground">
          <PinIcon v-if="section.pinned" aria-hidden="true" class="size-3" />
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
              <span
                class="line-clamp-1 text-sm"
                :class="conversation.id === activeId ? 'font-medium' : undefined"
              >
                {{ conversation.title }}
              </span>
              <span class="mt-0.5 block text-xs text-muted-foreground">
                {{ conversation.updatedAt }}
              </span>
            </button>

            <!--
              省略号菜单是行按钮的兄弟节点，点它不会触发行选中；
              行级底色挂在 li 上，两个按钮自身不画背景，hover 任一处都是同一块圆角底。
              `[--destructive]` 只在本菜单内把危险操作改成红色，不动全局 token。
            -->
            <DropdownMenu>
              <DropdownMenuTrigger as-child>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  type="button"
                  class="shrink-0 text-muted-foreground hover:bg-transparent hover:text-foreground aria-expanded:bg-transparent"
                  :aria-label="`打开「${conversation.title}」的会话菜单`"
                >
                  <EllipsisIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" class="w-40 [--destructive:#dc2626]">
                <DropdownMenuItem @select="emit('togglePin', conversation.id)">
                  {{ conversation.pinned === true ? "取消置顶" : "置顶" }}
                </DropdownMenuItem>
                <DropdownMenuItem @select="emit('requestRename', conversation.id)">
                  修改标题
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  @select="emit('requestDelete', conversation.id)"
                >
                  删除会话
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>
