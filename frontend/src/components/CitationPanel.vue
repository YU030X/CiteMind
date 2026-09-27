<script setup lang="ts">
import { computed } from "vue";

import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import type { Citation } from "@/api/types";
import { describeLocator } from "@/labels";
import { state } from "@/state/store";

/**
 * 引用详情：内容全部来自 `GET /citations/{id}`，每次打开都重新向服务端取，
 * 权限与来源存在性由服务端复核；本组件只展示服务端返回的文本。
 */
const citation = computed<Citation | null>(() => state.citation);
const locator = computed(() =>
  citation.value === null ? null : describeLocator(citation.value.locator),
);
</script>

<template>
  <div class="flex flex-col gap-3">
    <template v-if="state.citationLoading">
      <Skeleton class="h-6 w-40" />
      <Skeleton class="h-16 w-full" />
      <Skeleton class="h-24 w-full" />
    </template>

    <Alert v-else-if="state.citationError !== ''" variant="destructive">
      <AlertTitle>无法读取引用</AlertTitle>
      <AlertDescription>{{ state.citationError }}</AlertDescription>
    </Alert>

    <template v-else-if="citation !== null">
      <div class="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">{{ citation.displayLabel }}</Badge>
        <span class="text-sm font-medium">{{ citation.documentTitle }}</span>
      </div>

      <dl class="grid grid-cols-[4rem_1fr] gap-y-1 text-xs">
        <dt class="text-muted-foreground">版本</dt>
        <dd>v{{ citation.version }}</dd>
        <dt class="text-muted-foreground">定位</dt>
        <dd>{{ locator?.text }}</dd>
        <dt class="text-muted-foreground">引用 ID</dt>
        <dd class="font-mono break-all">{{ citation.citationId }}</dd>
      </dl>

      <Separator />

      <div class="flex flex-col gap-1.5">
        <p class="text-xs text-muted-foreground">资料原文片段</p>
        <p class="rounded-md border bg-secondary p-3 text-sm leading-6 whitespace-pre-wrap">
          {{ citation.quote }}
        </p>
      </div>

      <p class="text-xs leading-5 text-muted-foreground">
        定位与原文片段由服务端从已保存的资料块映射，模型不能提交 URL、页码或数据库 ID。
      </p>
    </template>

    <p v-else class="text-sm text-muted-foreground">还没有打开任何引用。</p>
  </div>
</template>
