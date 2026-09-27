<script setup lang="ts">
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import type { Citation } from "@/demos/data";

defineProps<{ citation: Citation }>();
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="flex flex-wrap items-center gap-2">
      <Badge variant="secondary">[{{ citation.marker }}]</Badge>
      <span class="text-sm font-medium">{{ citation.documentTitle }}</span>
    </div>

    <dl class="grid grid-cols-[4rem_1fr] gap-y-1 text-xs">
      <dt class="text-muted-foreground">版本</dt>
      <dd>{{ citation.version }}</dd>
      <dt class="text-muted-foreground">定位</dt>
      <dd>{{ citation.locator }}</dd>
      <dt class="text-muted-foreground">引用 ID</dt>
      <dd class="font-mono">{{ citation.id }}</dd>
    </dl>

    <Separator />

    <div class="flex flex-col gap-1.5">
      <p class="text-xs text-muted-foreground">chunk 原文</p>
      <p class="rounded-md border bg-secondary p-3 text-sm leading-6">{{ citation.quote }}</p>
    </div>

    <p class="text-xs text-muted-foreground">
      演示引用：标题、版本、定位与引文均为静态样例，未从服务端读取。
    </p>
  </div>
</template>
