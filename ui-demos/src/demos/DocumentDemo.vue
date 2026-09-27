<script setup lang="ts">
import { computed, ref } from "vue";
import { EllipsisIcon, InfoIcon, SearchIcon, UploadIcon } from "@lucide/vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  documents,
  formatLabel,
  statusBadgeVariant,
  statusLabel,
  type DocumentFormat,
  type DocumentStatus,
} from "./data";

const rows = ref(documents.map((row) => ({ ...row })));
const keyword = ref("");
const statusFilter = ref<DocumentStatus | "all">("all");
const formatFilter = ref<DocumentFormat | "all">("all");
const uploadOpen = ref(false);
const uploadTitle = ref("");
const uploadFormat = ref<DocumentFormat>("markdown");
const notice = ref("");

const filtered = computed(() =>
  rows.value.filter((row) => {
    const needle = keyword.value.trim().toLowerCase();
    if (needle !== "" && !row.title.toLowerCase().includes(needle)) return false;
    if (statusFilter.value !== "all" && row.status !== statusFilter.value) return false;
    if (formatFilter.value !== "all" && row.format !== formatFilter.value) return false;
    return true;
  }),
);

function openUpload() {
  uploadTitle.value = "";
  uploadFormat.value = "markdown";
  uploadOpen.value = true;
}

function submitUpload() {
  notice.value = `演示：未接入后端，未真正上传「${uploadTitle.value.trim()}」。`;
  uploadOpen.value = false;
}

function handleAction(action: string, title: string) {
  notice.value = `演示：「${action} · ${title}」只切换界面提示，未调用后端接口。`;
}
</script>

<template>
  <section class="flex flex-col gap-4">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div class="flex flex-col gap-1">
        <h1 class="text-lg font-medium">文档管理</h1>
        <p class="text-sm text-muted-foreground">
          按住文档的接收与处理状态查看；当前仅支持 Markdown 与 PDF。
        </p>
      </div>

      <Button type="button" class="hover:bg-primary-hover" @click="openUpload">
        <UploadIcon />
        上传文档
      </Button>
    </div>

    <Alert v-if="notice !== ''">
      <InfoIcon />
      <AlertTitle>演示操作</AlertTitle>
      <AlertDescription>{{ notice }}</AlertDescription>
    </Alert>

    <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <div class="relative sm:w-72">
        <SearchIcon
          class="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          id="document-search"
          v-model="keyword"
          class="pl-8"
          placeholder="搜索文档标题"
          aria-label="搜索文档标题"
          type="search"
        />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-muted-foreground">筛选</span>
        <Select v-model="statusFilter">
          <SelectTrigger size="sm" class="w-32" aria-label="按状态筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" class="w-32">
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="ready">可用</SelectItem>
            <SelectItem value="queued">排队中</SelectItem>
            <SelectItem value="parsing">解析中</SelectItem>
            <SelectItem value="failed">失败</SelectItem>
          </SelectContent>
        </Select>

        <Select v-model="formatFilter">
          <SelectTrigger size="sm" class="w-32" aria-label="按类型筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" class="w-32">
            <SelectItem value="all">全部类型</SelectItem>
            <SelectItem value="markdown">Markdown</SelectItem>
            <SelectItem value="pdf">PDF</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>

    <p class="text-xs text-muted-foreground">
      共 {{ filtered.length }} 个文档（静态样例，不是服务端统计）。
    </p>

    <div class="rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead class="pl-3">名称</TableHead>
            <TableHead>类型</TableHead>
            <TableHead>状态</TableHead>
            <TableHead class="hidden md:table-cell">版本</TableHead>
            <TableHead class="hidden md:table-cell">大小</TableHead>
            <TableHead class="hidden sm:table-cell">更新于</TableHead>
            <TableHead class="hidden lg:table-cell">上传者</TableHead>
            <TableHead class="pr-3 text-right">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow v-for="row in filtered" :key="row.id">
            <TableCell class="max-w-64 truncate pl-3 font-medium">{{ row.title }}</TableCell>
            <TableCell>
              <Badge variant="outline">{{ formatLabel[row.format] }}</Badge>
            </TableCell>
            <TableCell>
              <Badge :variant="statusBadgeVariant[row.status]">
                {{ statusLabel[row.status] }}
              </Badge>
            </TableCell>
            <TableCell class="hidden md:table-cell text-muted-foreground">{{ row.version }}</TableCell>
            <TableCell class="hidden md:table-cell text-muted-foreground">{{ row.sizeLabel }}</TableCell>
            <TableCell class="hidden sm:table-cell text-muted-foreground">{{ row.updatedAt }}</TableCell>
            <TableCell class="hidden lg:table-cell text-muted-foreground">{{ row.uploadedBy }}</TableCell>
            <TableCell class="pr-3 text-right">
              <DropdownMenu>
                <DropdownMenuTrigger as-child>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    type="button"
                    :aria-label="`打开「${row.title}」的操作菜单`"
                  >
                    <EllipsisIcon />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" class="w-44">
                  <DropdownMenuItem @select="handleAction('查看详情', row.title)">
                    查看详情
                  </DropdownMenuItem>
                  <DropdownMenuItem @select="handleAction('上传新版本', row.title)">
                    上传新版本
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    @select="handleAction('删除', row.title)"
                  >
                    删除文档
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </TableCell>
          </TableRow>
          <TableRow v-if="filtered.length === 0">
            <TableCell colspan="8" class="py-10 text-center text-muted-foreground">
              没有匹配当前筛选条件的文档。
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>

    <Dialog v-model:open="uploadOpen">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>上传文档</DialogTitle>
          <DialogDescription>演示表单：不会上传文件，也不会启动任何处理任务。</DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-4">
          <div
            class="flex flex-col items-center gap-2 rounded-md border border-dashed bg-secondary px-4 py-6 text-center"
          >
            <UploadIcon class="size-5 text-muted-foreground" />
            <p class="text-sm">把文件拖到这里，或点击选择文件</p>
            <p class="text-xs text-muted-foreground">仅支持 Markdown（.md）与 PDF（.pdf）</p>
            <Button variant="outline" size="sm" type="button" disabled>
              选择文件（演示不可用）
            </Button>
          </div>

          <div class="flex flex-col gap-2">
            <Label for="upload-title">标题</Label>
            <Input id="upload-title" v-model="uploadTitle" placeholder="例如：员工手册 v3" />
          </div>

          <div class="flex flex-col gap-2">
            <Label for="upload-format">类型</Label>
            <Select v-model="uploadFormat">
              <SelectTrigger id="upload-format" class="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                <SelectItem value="markdown">Markdown</SelectItem>
                <SelectItem value="pdf">PDF</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" type="button" @click="uploadOpen = false">取消</Button>
          <Button
            type="button"
            class="hover:bg-primary-hover"
            :disabled="uploadTitle.trim() === ''"
            @click="submitUpload"
          >
            确认上传
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>
