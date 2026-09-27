<script setup lang="ts">
import { computed, ref } from "vue";
import { InfoIcon, PlusIcon, SearchIcon } from "@lucide/vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
import { Skeleton } from "@/components/ui/skeleton";
import { kbRoleLabel } from "@/labels";
import {
  createKnowledgeBase,
  loadKnowledgeBases,
  selectKnowledgeBase,
  state,
} from "@/state/store";

const emit = defineEmits<{ openDocuments: [] }>();

const keyword = ref("");
const createOpen = ref(false);
const draftName = ref("");

const sessionUser = computed(() => state.session?.user ?? null);
const canCreate = computed(() => sessionUser.value?.isAdmin === true);

const filtered = computed(() => {
  const needle = keyword.value.trim().toLowerCase();
  if (needle === "") return state.knowledgeBases;
  return state.knowledgeBases.filter((kb) => kb.name.toLowerCase().includes(needle));
});

/** 只选择知识库并切到文档管理：创建、成员与权限都仍由服务端判定。 */
function openKnowledgeBase(kbId: string): void {
  selectKnowledgeBase(kbId);
  emit("openDocuments");
}

function openCreate(): void {
  draftName.value = "";
  createOpen.value = true;
}

async function submitCreate(): Promise<void> {
  if (draftName.value.trim() === "") return;
  if (await createKnowledgeBase(draftName.value)) {
    draftName.value = "";
    createOpen.value = false;
  }
}
</script>

<template>
  <section class="flex flex-col gap-4">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div class="flex flex-col gap-1">
        <h1 class="text-lg font-medium">知识库</h1>
        <p class="text-sm text-muted-foreground">
          这里只列出你有成员权限的知识库；每个知识库独立存放资料，当前支持 Markdown 与 PDF。
        </p>
      </div>

      <div class="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div class="relative">
          <SearchIcon
            class="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            id="kb-search"
            v-model="keyword"
            class="pl-8 sm:w-64"
            placeholder="按名称筛选"
            aria-label="按名称筛选知识库"
            type="search"
          />
        </div>
        <Button
          v-if="canCreate"
          type="button"
          class="hover:bg-primary-hover"
          @click="openCreate"
        >
          <PlusIcon />
          新建知识库
        </Button>
      </div>
    </div>

    <Alert>
      <InfoIcon />
      <AlertTitle>权限说明</AlertTitle>
      <AlertDescription>
        角色与可见范围由服务端判定：只有管理员能创建知识库，成员与权限目前只能由后端授予。
      </AlertDescription>
    </Alert>

    <p v-if="state.knowledgeBasesLoading" class="text-xs text-muted-foreground">
      正在载入知识库…
    </p>
    <p v-else class="text-xs text-muted-foreground">
      共 {{ filtered.length }} 个知识库（服务端返回，本片不分页）。
    </p>

    <Alert v-if="state.knowledgeBasesError !== ''" variant="destructive">
      <AlertTitle>无法载入知识库</AlertTitle>
      <AlertDescription class="flex flex-col items-start gap-2">
        <span>{{ state.knowledgeBasesError }}</span>
        <Button variant="outline" size="sm" type="button" @click="loadKnowledgeBases">
          重试
        </Button>
      </AlertDescription>
    </Alert>

    <Skeleton v-if="state.knowledgeBasesLoading" class="h-28 w-full" />

    <div
      v-else-if="state.knowledgeBases.length === 0"
      class="rounded-md border border-dashed bg-card p-8 text-center text-sm text-muted-foreground"
    >
      你还没有可访问的知识库。{{
        canCreate ? "可以在上方新建一个。" : "请联系管理员授予成员权限。"
      }}
    </div>

    <div
      v-else-if="filtered.length === 0"
      class="rounded-md border border-dashed bg-card p-8 text-center text-sm text-muted-foreground"
    >
      没有匹配「{{ keyword }}」的知识库。
    </div>

    <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <Card v-for="kb in filtered" :key="kb.id" class="gap-4 py-5">
        <CardHeader>
          <CardTitle class="truncate">{{ kb.name }}</CardTitle>
          <CardAction>
            <Badge v-if="kb.id === state.activeKbId" variant="secondary">当前</Badge>
          </CardAction>
        </CardHeader>

        <CardContent class="flex flex-col gap-3">
          <div class="flex flex-wrap items-center gap-2">
            <Badge variant="outline">{{ kbRoleLabel(kb.role) }}</Badge>
          </div>
          <Button variant="outline" size="sm" type="button" @click="openKnowledgeBase(kb.id)">
            打开文档管理
          </Button>
        </CardContent>
      </Card>
    </div>

    <Dialog v-model:open="createOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>新建知识库</DialogTitle>
          <DialogDescription>
            创建请求由服务端判定管理员权限，并在同一事务写入你的所有者成员行。
          </DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-2">
          <Label for="kb-name">名称</Label>
          <Input
            id="kb-name"
            v-model="draftName"
            placeholder="例如：员工手册"
            autocomplete="off"
            :disabled="state.createKnowledgeBasePending"
            @keydown.enter.prevent="submitCreate"
          />
        </div>

        <Alert v-if="state.createKnowledgeBaseError !== ''" variant="destructive">
          <AlertTitle>创建失败</AlertTitle>
          <AlertDescription>{{ state.createKnowledgeBaseError }}</AlertDescription>
        </Alert>

        <DialogFooter>
          <Button variant="outline" type="button" @click="createOpen = false">取消</Button>
          <Button
            type="button"
            class="hover:bg-primary-hover"
            :disabled="state.createKnowledgeBasePending || draftName.trim() === ''"
            @click="submitCreate"
          >
            {{ state.createKnowledgeBasePending ? "创建中…" : "创建" }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>
