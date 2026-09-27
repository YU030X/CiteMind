<script setup lang="ts">
import { computed, ref } from "vue";
import { EllipsisIcon, InfoIcon, PlusIcon, SearchIcon } from "@lucide/vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { formatLabel, knowledgeBases, type KnowledgeBase } from "./data";

const items = ref<KnowledgeBase[]>(knowledgeBases.map((item) => ({ ...item })));
const keyword = ref("");
const createOpen = ref(false);
const draftName = ref("");
const draftDescription = ref("");
const notice = ref("");

const filtered = computed(() => {
  const needle = keyword.value.trim().toLowerCase();
  if (needle === "") return items.value;
  return items.value.filter((item) =>
    `${item.name} ${item.description}`.toLowerCase().includes(needle),
  );
});

function openCreate() {
  draftName.value = "";
  draftDescription.value = "";
  createOpen.value = true;
}

function createKnowledgeBase() {
  const name = draftName.value.trim();
  if (name === "") return;
  const description = draftDescription.value.trim();
  items.value = [
    {
      id: `demo-kb-${items.value.length + 1}`,
      name,
      description: description === "" ? "（演示新建，未填写描述）" : description,
      role: "拥有者",
      documentCount: 0,
      updatedAt: "刚刚",
      formats: [],
    },
    ...items.value,
  ];
  notice.value = `演示：已在本地列表中加入「${name}」，未调用后端接口。`;
  createOpen.value = false;
}

function handleAction(action: string, name: string) {
  notice.value = `演示：「${action} · ${name}」只切换界面提示，未调用后端接口。`;
}
</script>

<template>
  <section class="flex flex-col gap-4">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div class="flex flex-col gap-1">
        <h1 class="text-lg font-medium">知识库</h1>
        <p class="text-sm text-muted-foreground">
          每个知识库独立存放资料与引用；当前仅支持 Markdown 与 PDF。
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
            placeholder="搜索知识库名称或描述"
            aria-label="搜索知识库"
            type="search"
          />
        </div>
        <Button
          type="button"
          class="hover:bg-primary-hover"
          @click="openCreate"
        >
          <PlusIcon />
          新建知识库
        </Button>
      </div>
    </div>

    <Alert v-if="notice !== ''">
      <InfoIcon />
      <AlertTitle>演示操作</AlertTitle>
      <AlertDescription>{{ notice }}</AlertDescription>
    </Alert>

    <p class="text-xs text-muted-foreground">
      共 {{ filtered.length }} 个知识库（静态样例，不是服务端统计）。
    </p>

    <div
      v-if="filtered.length === 0"
      class="rounded-md border border-dashed bg-card p-8 text-center text-sm text-muted-foreground"
    >
      没有匹配「{{ keyword }}」的知识库。
    </div>

    <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <Card v-for="item in filtered" :key="item.id" class="gap-4 py-5">
        <CardHeader>
          <CardTitle>{{ item.name }}</CardTitle>
          <CardAction>
            <DropdownMenu>
              <DropdownMenuTrigger as-child>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  type="button"
                  :aria-label="`打开「${item.name}」的操作菜单`"
                >
                  <EllipsisIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent class="w-40">
                <DropdownMenuItem @select="handleAction('打开', item.name)">
                  打开知识库
                </DropdownMenuItem>
                <DropdownMenuItem @select="handleAction('管理成员', item.name)">
                  管理成员
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  @select="handleAction('删除', item.name)"
                >
                  删除知识库
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </CardAction>
          <CardDescription class="line-clamp-2">{{ item.description }}</CardDescription>
        </CardHeader>

        <CardContent class="flex flex-col gap-2">
          <div class="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{{ item.role }}</Badge>
            <Badge v-for="format in item.formats" :key="format" variant="outline">
              {{ formatLabel[format] }}
            </Badge>
          </div>
          <p class="text-xs text-muted-foreground">
            {{ item.documentCount }} 个文档 · 更新于 {{ item.updatedAt }}
          </p>
        </CardContent>
      </Card>
    </div>

    <Dialog v-model:open="createOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>新建知识库</DialogTitle>
          <DialogDescription>
            演示表单：不会创建真实知识库，也不会写入任何数据。
          </DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-4">
          <div class="flex flex-col gap-2">
            <Label for="kb-name">名称</Label>
            <Input
              id="kb-name"
              v-model="draftName"
              placeholder="例如：员工手册"
              autocomplete="off"
            />
          </div>
          <div class="flex flex-col gap-2">
            <Label for="kb-description">描述（可选）</Label>
            <Textarea
              id="kb-description"
              v-model="draftDescription"
              class="min-h-20"
              placeholder="一句话说明这个知识库收什么资料"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" type="button" @click="createOpen = false">
            取消
          </Button>
          <Button
            type="button"
            class="hover:bg-primary-hover"
            :disabled="draftName.trim() === ''"
            @click="createKnowledgeBase"
          >
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>
