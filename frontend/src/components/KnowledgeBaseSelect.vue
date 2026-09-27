<script setup lang="ts">
import { computed } from "vue";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { kbRoleLabel } from "@/labels";
import { selectKnowledgeBase, state } from "@/state/store";

/**
 * 知识库选择器：选择结果写入工作台状态，文档页与问答页共用同一个 `activeKbId`。
 * 作用范围与角色始终由服务端判定，这里只做选择。
 */
const disabled = computed(
  () => state.knowledgeBasesLoading || state.knowledgeBases.length === 0,
);
const placeholder = computed(() =>
  state.knowledgeBases.length === 0 ? "暂无可用知识库" : "选择知识库",
);

function onChange(value: unknown): void {
  if (typeof value === "string" && value !== "") selectKnowledgeBase(value);
}
</script>

<template>
  <Select :model-value="state.activeKbId" :disabled="disabled" @update:model-value="onChange">
    <SelectTrigger size="sm" class="w-56" aria-label="选择知识库">
      <SelectValue :placeholder="placeholder" />
    </SelectTrigger>
    <SelectContent position="popper">
      <SelectItem v-for="kb in state.knowledgeBases" :key="kb.id" :value="kb.id">
        {{ kb.name }}（{{ kbRoleLabel(kb.role) }}）
      </SelectItem>
    </SelectContent>
  </Select>
</template>
