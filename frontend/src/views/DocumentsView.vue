<script setup lang="ts">
import { computed, ref } from "vue";
import {
  EllipsisIcon,
  InfoIcon,
  RefreshCwIcon,
  SearchIcon,
  UploadIcon,
} from "@lucide/vue";

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
import KnowledgeBaseSelect from "@/components/KnowledgeBaseSelect.vue";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, describeError } from "@/api/client";
import { api } from "@/api/endpoints";
import type { DocumentSummary, LifecycleStatus, SourceType } from "@/api/types";
import {
  formatTime,
  isTerminalJobStatus,
  jobErrorLabel,
  jobLabel,
  kbRoleLabel,
  lifecycleBadgeVariant,
  lifecycleLabel,
  sourceTypeLabel,
  versionLabel,
} from "@/labels";
import {
  activeKnowledgeBase,
  closeCitation,
  refreshDocuments,
  refreshMessages,
  state,
} from "@/state/store";

const currentKb = computed(() => activeKnowledgeBase());
const canEdit = computed(() => {
  const role = currentKb.value?.role;
  return role === "EDITOR" || role === "OWNER";
});
const canDelete = computed(() => currentKb.value?.role === "OWNER");

const keyword = ref("");
const statusFilter = ref<LifecycleStatus | "all">("all");
const typeFilter = ref<SourceType | "all">("all");

/** 服务端本片不分页，这里只做本地筛选，不伪造总数。 */
const visibleDocuments = computed(() =>
  state.documents.filter((item) => {
    const needle = keyword.value.trim().toLowerCase();
    if (needle !== "" && !item.title.toLowerCase().includes(needle)) return false;
    if (statusFilter.value !== "all" && item.lifecycleStatus !== statusFilter.value) return false;
    if (typeFilter.value !== "all" && item.sourceType !== typeFilter.value) return false;
    return true;
  }),
);

const hasPendingWork = computed(() =>
  state.documents.some((item) => {
    const job = item.latestJob;
    if (job !== null && !isTerminalJobStatus(job.status)) return true;
    return item.lifecycleStatus === "CREATED" || item.lifecycleStatus === "INDEXING";
  }),
);

const STATUS_FILTERS: LifecycleStatus[] = ["CREATED", "INDEXING", "READY", "FAILED"];

function versionLine(item: DocumentSummary): string {
  const active = item.activeVersion;
  const latest = item.latestVersion;
  const activeText =
    active === null ? "无可用版本" : `可用 v${active.versionNo}（${versionLabel(active.status)}）`;
  if (latest === null) return activeText;
  const latestText = `最新 v${latest.versionNo}（${versionLabel(latest.status)}）`;
  return latest.id === active?.id ? `${latestText} · 已发布` : `${activeText} · ${latestText}`;
}

/**
 * 状态单元格补充说明：窄屏下「版本」与「最新任务」列会被隐藏，这里复用现有 labels
 * 把最新一次失败或需 OCR 的原因放到始终可见的状态列，并区分首版失败与更新失败。
 */
function statusDetail(item: DocumentSummary): string {
  const latest = item.latestVersion;
  if (latest === null) return "";
  const job = item.latestJob;
  const error = job === null ? "" : jobErrorLabel(job.errorCode);
  // 首版就失败或需要 OCR：当前没有可用版本，直接显示原因。
  if (item.activeVersion === null) {
    if (latest.status === "FAILED" || latest.status === "NEEDS_OCR") {
      return error === "" ? versionLabel(latest.status) : error;
    }
    return "";
  }
  // 已有可用版本但新版本更新失败或需 OCR：明确区分更新原因与「旧版本仍可用」。
  if (latest.id !== item.activeVersion.id) {
    if (latest.status === "FAILED") {
      const reason = error === "" ? versionLabel(latest.status) : error;
      return `更新失败：${reason}（旧版本 v${item.activeVersion.versionNo} 仍可用）`;
    }
    if (latest.status === "NEEDS_OCR") {
      const reason = error === "" ? versionLabel(latest.status) : error;
      return `更新需 OCR：${reason}（旧版本 v${item.activeVersion.versionNo} 仍可用）`;
    }
  }
  return "";
}

function pendingWorkLabel(item: DocumentSummary): string {
  const job = item.latestJob;
  if (job === null) return "—";
  const stage = jobLabel(job.status);
  const error = jobErrorLabel(job.errorCode);
  return error === "" ? stage : `${stage} · ${error}`;
}

// --- 上传新文档 ---------------------------------------------------------------

const uploadOpen = ref(false);
const uploadTitle = ref("");
const uploadFile = ref<File | null>(null);
const uploadPending = ref(false);
const uploadError = ref("");
const uploadNotice = ref("");
const uploadInputKey = ref(0);
// 同一次提交的重试复用同一个 Idempotency-Key；标题或文件变化后必须换新 key。
let pendingUpload: { key: string; signature: string } | null = null;

function uploadSignature(title: string, file: File): string {
  return [title, file.name, String(file.size), String(file.lastModified)].join("\u0000");
}

function openUpload(): void {
  uploadTitle.value = "";
  uploadFile.value = null;
  uploadError.value = "";
  uploadNotice.value = "";
  pendingUpload = null;
  uploadInputKey.value += 1;
  uploadOpen.value = true;
}

function onUploadFileChange(event: Event): void {
  const target = event.target as HTMLInputElement | null;
  const file = target?.files?.[0] ?? null;
  uploadFile.value = file;
  uploadError.value = "";
  if (file !== null && uploadTitle.value.trim() === "") {
    uploadTitle.value = file.name.replace(/\.(md|markdown|pdf)$/i, "");
  }
}

async function submitUpload(): Promise<void> {
  if (uploadPending.value) return;
  const kbId = state.activeKbId;
  const file = uploadFile.value;
  const title = uploadTitle.value.trim();
  if (kbId === "") {
    uploadError.value = "请先选择知识库";
    return;
  }
  if (file === null) {
    uploadError.value = "请选择 .md、.markdown 或 .pdf 文件";
    return;
  }
  if (title === "") {
    uploadError.value = "请填写文档标题";
    return;
  }

  uploadPending.value = true;
  uploadError.value = "";
  uploadNotice.value = "";

  const signature = uploadSignature(title, file);
  if (pendingUpload === null || pendingUpload.signature !== signature) {
    pendingUpload = { key: crypto.randomUUID(), signature };
  }
  const form = new FormData();
  form.append("title", title);
  form.append("file", file);

  try {
    await api.uploadDocument(kbId, form, pendingUpload.key);
    pendingUpload = null;
    uploadOpen.value = false;
    uploadNotice.value = `「${title}」上传已受理：202 只表示文件与任务已落库，解析与索引尚未完成。`;
    await refreshDocuments();
  } catch (error) {
    // 失败后保留文件与 key：再次点击“上传”会用同一个 Idempotency-Key 重试同一次提交。
    uploadError.value = `${describeError(error)}。再次点击“上传”会用同一个 Idempotency-Key 重试同一次提交。`;
  } finally {
    uploadPending.value = false;
  }
}

// --- 上传新版本 ---------------------------------------------------------------

const versionTarget = ref<DocumentSummary | null>(null);
const versionOpen = ref(false);
const versionFile = ref<File | null>(null);
const versionPending = ref(false);
const versionFormError = ref("");
const versionConflict = ref("");
const versionInputKey = ref(0);
// 一次提交失败时同 key 重试；`expectedVersionId` 变化后必须换新请求。
let pendingVersion: { key: string; signature: string } | null = null;

function openVersionEditor(item: DocumentSummary): void {
  versionTarget.value = item;
  versionFile.value = null;
  versionFormError.value = "";
  versionConflict.value = "";
  pendingVersion = null;
  versionInputKey.value += 1;
  versionOpen.value = true;
}

function closeVersionEditor(): void {
  versionOpen.value = false;
  versionTarget.value = null;
  versionFile.value = null;
  versionFormError.value = "";
  pendingVersion = null;
}

function onVersionFileChange(event: Event): void {
  const target = event.target as HTMLInputElement | null;
  versionFile.value = target?.files?.[0] ?? null;
  versionFormError.value = "";
}

async function submitVersion(): Promise<void> {
  const item = versionTarget.value;
  if (item === null || versionPending.value) return;
  const file = versionFile.value;
  const active = item.activeVersion;
  if (active === null) {
    versionFormError.value = "该文档还没有可用版本，不能上传新版本";
    return;
  }
  if (file === null) {
    versionFormError.value = "请选择新版本的 .md、.markdown 或 .pdf 文件";
    return;
  }

  versionPending.value = true;
  versionFormError.value = "";
  versionConflict.value = "";

  const signature = [item.id, active.id, file.name, String(file.size), String(file.lastModified)].join(
    "\u0000",
  );
  if (pendingVersion === null || pendingVersion.signature !== signature) {
    pendingVersion = { key: crypto.randomUUID(), signature };
  }
  const form = new FormData();
  form.append("title", item.title);
  form.append("expectedVersionId", active.id);
  form.append("file", file);

  try {
    await api.uploadDocumentVersion(item.id, form, pendingVersion.key);
    pendingVersion = null;
    closeVersionEditor();
    uploadNotice.value = `「${item.title}」新版本已受理：当前可用版本不会立即变化。`;
    await refreshDocuments();
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // 版本或幂等冲突必须换新的 expectedVersionId 重新提交，旧 key 不再复用。
      pendingVersion = null;
      closeVersionEditor();
      versionConflict.value = `${describeError(error)}（列表已刷新，请确认当前版本后重新提交）`;
      await refreshDocuments();
    } else {
      versionFormError.value = describeError(error);
    }
  } finally {
    versionPending.value = false;
  }
}

// --- 详情 ---------------------------------------------------------------------

const detailOpen = ref(false);
const detailLoading = ref(false);
const detailError = ref("");
const detailDocument = ref<DocumentSummary | null>(null);

async function openDetail(documentId: string): Promise<void> {
  detailOpen.value = true;
  detailLoading.value = true;
  detailError.value = "";
  detailDocument.value = null;
  try {
    // 每次都重新请求 `GET /documents/{id}`：权限与删除状态由服务端复核。
    detailDocument.value = await api.getDocument(documentId);
  } catch (error) {
    detailError.value = describeError(error);
  } finally {
    detailLoading.value = false;
  }
}

// --- 删除 ---------------------------------------------------------------------

const deleteTarget = ref<DocumentSummary | null>(null);
const deleteOpen = ref(false);
const deletePending = ref(false);
const deleteError = ref("");
const deleteNotice = ref("");

function requestDelete(item: DocumentSummary): void {
  deleteTarget.value = item;
  deleteError.value = "";
  deleteNotice.value = "";
  deleteOpen.value = true;
}

async function confirmDelete(): Promise<void> {
  const item = deleteTarget.value;
  if (item === null || deletePending.value) return;
  deletePending.value = true;
  deleteError.value = "";
  try {
    await api.deleteDocument(item.id);
    deleteOpen.value = false;
    deleteTarget.value = null;
    deleteNotice.value = `「${item.title}」已删除：该文档的检索与引用立即失效，操作不可撤销。`;
    // 文档变化后同时刷新文档列表、当前会话消息与引用：服务端会隐藏已删除来源的回答与引用。
    await refreshDocuments();
    await refreshMessages();
    closeCitation();
  } catch (error) {
    deleteError.value = describeError(error);
  } finally {
    deletePending.value = false;
  }
}
</script>

<template>
  <section class="flex flex-col gap-4">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div class="flex flex-col gap-1">
        <h1 class="text-lg font-medium">文档管理</h1>
        <p class="text-sm text-muted-foreground">
          查看当前知识库的接收与处理状态；当前仅支持 Markdown 与 PDF。
        </p>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <KnowledgeBaseSelect />
        <Button
          type="button"
          class="hover:bg-primary-hover"
          :disabled="!canEdit"
          :title="canEdit ? undefined : '当前角色不能上传或更新资料'"
          @click="openUpload"
        >
          <UploadIcon />
          上传文档
        </Button>
      </div>
    </div>

    <Alert>
      <InfoIcon />
      <AlertTitle>角色与阶段</AlertTitle>
      <AlertDescription>
        <template v-if="currentKb === null">
          还没有选择知识库，请先在右上角选择。
        </template>
        <template v-else-if="canEdit">
          当前角色：{{ kbRoleLabel(currentKb.role) }}，可以上传文档与提交新版本；删除文档仅限所有者。
        </template>
        <template v-else>
          当前角色：{{ kbRoleLabel(currentKb.role) }}，不能上传或更新资料；权限以服务端判定为准。
        </template>
      </AlertDescription>
    </Alert>

    <Alert v-if="uploadNotice !== ''">
      <AlertTitle>操作已受理</AlertTitle>
      <AlertDescription>{{ uploadNotice }}</AlertDescription>
    </Alert>
    <Alert v-if="versionConflict !== ''" variant="destructive">
      <AlertTitle>版本冲突</AlertTitle>
      <AlertDescription>{{ versionConflict }}</AlertDescription>
    </Alert>
    <Alert v-if="deleteNotice !== ''">
      <AlertTitle>删除结果</AlertTitle>
      <AlertDescription>{{ deleteNotice }}</AlertDescription>
    </Alert>
    <Alert v-if="deleteError !== ''" variant="destructive">
      <AlertTitle>删除失败</AlertTitle>
      <AlertDescription>{{ deleteError }}</AlertDescription>
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
          placeholder="按标题筛选"
          aria-label="按标题筛选文档"
          type="search"
        />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-muted-foreground">筛选</span>
        <Select v-model="statusFilter">
          <SelectTrigger size="sm" class="w-36" aria-label="按状态筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem v-for="status in STATUS_FILTERS" :key="status" :value="status">
              {{ lifecycleLabel(status) }}
            </SelectItem>
          </SelectContent>
        </Select>

        <Select v-model="typeFilter">
          <SelectTrigger size="sm" class="w-32" aria-label="按类型筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value="all">全部类型</SelectItem>
            <SelectItem value="markdown">Markdown</SelectItem>
            <SelectItem value="pdf">PDF</SelectItem>
          </SelectContent>
        </Select>

        <Button
          variant="outline"
          size="icon-sm"
          type="button"
          aria-label="刷新文档列表"
          title="刷新"
          :disabled="state.documentsLoading"
          @click="refreshDocuments"
        >
          <RefreshCwIcon />
        </Button>
      </div>
    </div>

    <p class="text-xs text-muted-foreground">
      共 {{ visibleDocuments.length }} 个文档（服务端返回，本片不分页）。<template
        v-if="hasPendingWork"
      >
        有任务进行中，列表每 5 秒自动刷新。</template
      >
    </p>

    <Skeleton v-if="state.documentsLoading" class="h-40 w-full" />

    <Alert v-else-if="state.documentsError !== ''" variant="destructive">
      <AlertTitle>无法载入文档列表</AlertTitle>
      <AlertDescription class="flex flex-col items-start gap-2">
        <span>{{ state.documentsError }}</span>
        <Button variant="outline" size="sm" type="button" @click="refreshDocuments">重试</Button>
      </AlertDescription>
    </Alert>

    <p
      v-else-if="state.activeKbId === ''"
      class="rounded-md border border-dashed bg-card p-8 text-center text-sm text-muted-foreground"
    >
      先选择一个知识库。
    </p>

    <div v-else class="rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead class="pl-3">名称</TableHead>
            <TableHead>类型</TableHead>
            <TableHead>状态</TableHead>
            <TableHead class="hidden md:table-cell">版本</TableHead>
            <TableHead class="hidden lg:table-cell">最新任务</TableHead>
            <TableHead class="hidden sm:table-cell">更新于</TableHead>
            <TableHead class="pr-3 text-right">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow v-for="item in visibleDocuments" :key="item.id">
            <TableCell class="max-w-64 truncate pl-3 font-medium" :title="item.title">
              {{ item.title }}
            </TableCell>
            <TableCell>
              <Badge variant="outline">{{ sourceTypeLabel(item.sourceType) }}</Badge>
            </TableCell>
            <TableCell>
              <div class="flex flex-col gap-1">
                <Badge :variant="lifecycleBadgeVariant(item.lifecycleStatus)">
                  {{ lifecycleLabel(item.lifecycleStatus) }}
                </Badge>
                <span v-if="statusDetail(item) !== ''" class="text-xs text-destructive">
                  {{ statusDetail(item) }}
                </span>
              </div>
            </TableCell>
            <TableCell class="hidden text-muted-foreground md:table-cell">
              {{ versionLine(item) }}
            </TableCell>
            <TableCell class="hidden text-muted-foreground lg:table-cell">
              {{ pendingWorkLabel(item) }}
            </TableCell>
            <TableCell class="hidden text-muted-foreground sm:table-cell">
              {{ formatTime(item.updatedAt) }}
            </TableCell>
            <TableCell class="pr-3 text-right">
              <DropdownMenu>
                <DropdownMenuTrigger as-child>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    type="button"
                    :aria-label="`打开「${item.title}」的操作菜单`"
                  >
                    <EllipsisIcon />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" class="w-48 [--destructive:#dc2626]">
                  <DropdownMenuItem @select="openDetail(item.id)">查看详情</DropdownMenuItem>
                  <DropdownMenuItem
                    :disabled="!canEdit || item.activeVersion === null"
                    :title="
                      canEdit
                        ? item.activeVersion === null
                          ? '该文档还没有可用版本'
                          : undefined
                        : '需要编辑者或所有者角色'
                    "
                    @select="openVersionEditor(item)"
                  >
                    上传新版本
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    :disabled="!canDelete"
                    :title="canDelete ? undefined : '只有所有者能删除文档'"
                    @select="requestDelete(item)"
                  >
                    删除文档
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </TableCell>
          </TableRow>
          <TableRow v-if="visibleDocuments.length === 0">
            <TableCell colspan="7" class="py-10 text-center text-muted-foreground">
              {{
                state.documents.length === 0
                  ? "这个知识库还没有文档。"
                  : "没有匹配当前筛选条件的文档。"
              }}
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>

    <Dialog v-model:open="uploadOpen">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>上传文档</DialogTitle>
          <DialogDescription>
            提交后返回 202 只表示文件与入库任务已持久化，不代表解析或索引完成。
          </DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-4">
          <label
            class="flex cursor-pointer flex-col items-center gap-2 rounded-md border border-dashed bg-secondary px-4 py-6 text-center"
          >
            <UploadIcon class="size-5 text-muted-foreground" />
            <span class="text-sm">
              {{ uploadFile === null ? "选择文件" : uploadFile.name }}
            </span>
            <span class="text-xs text-muted-foreground">
              仅支持 Markdown（.md / .markdown，UTF-8 文本）与 PDF（.pdf，≤ 20 MB）
            </span>
            <input
              :key="uploadInputKey"
              type="file"
              class="sr-only"
              accept=".md,.markdown,.pdf"
              :disabled="uploadPending"
              @change="onUploadFileChange"
            />
          </label>

          <div class="flex flex-col gap-2">
            <Label for="upload-title">标题</Label>
            <Input
              id="upload-title"
              v-model="uploadTitle"
              maxlength="500"
              placeholder="例如：员工手册 v3"
              :disabled="uploadPending"
            />
          </div>

          <Alert v-if="uploadError !== ''" variant="destructive">
            <AlertTitle>上传未受理</AlertTitle>
            <AlertDescription>{{ uploadError }}</AlertDescription>
          </Alert>
        </div>

        <DialogFooter>
          <Button variant="outline" type="button" @click="uploadOpen = false">取消</Button>
          <Button
            type="button"
            class="hover:bg-primary-hover"
            :disabled="uploadPending || uploadFile === null || uploadTitle.trim() === ''"
            @click="submitUpload"
          >
            {{ uploadPending ? "上传中…" : "确认上传" }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="versionOpen">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>上传新版本</DialogTitle>
          <DialogDescription>
            以当前可用版本 v{{ versionTarget?.activeVersion?.versionNo ?? "—" }} 作为期望值提交；
            版本在此期间变化时服务端返回 409，需要重新确认后提交。
          </DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-4">
          <p class="truncate text-sm font-medium">{{ versionTarget?.title ?? "" }}</p>

          <label
            class="flex cursor-pointer flex-col items-center gap-2 rounded-md border border-dashed bg-secondary px-4 py-6 text-center"
          >
            <UploadIcon class="size-5 text-muted-foreground" />
            <span class="text-sm">{{ versionFile === null ? "选择新版本文件" : versionFile.name }}</span>
            <span class="text-xs text-muted-foreground">
              仅支持 Markdown（.md / .markdown）与 PDF（.pdf，≤ 20 MB）
            </span>
            <input
              :key="versionInputKey"
              type="file"
              class="sr-only"
              accept=".md,.markdown,.pdf"
              :disabled="versionPending"
              @change="onVersionFileChange"
            />
          </label>

          <Alert v-if="versionFormError !== ''" variant="destructive">
            <AlertTitle>版本未受理</AlertTitle>
            <AlertDescription>{{ versionFormError }}</AlertDescription>
          </Alert>
        </div>

        <DialogFooter>
          <Button variant="outline" type="button" @click="closeVersionEditor">取消</Button>
          <Button
            type="button"
            class="hover:bg-primary-hover"
            :disabled="versionPending || versionFile === null"
            @click="submitVersion"
          >
            {{ versionPending ? "提交中…" : "提交新版本" }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="detailOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>文档详情</DialogTitle>
          <DialogDescription>
            来自 `GET /documents/{id}` 的实时结果，不包含文件路径、摘要或正文。
          </DialogDescription>
        </DialogHeader>

        <div v-if="detailLoading" class="flex flex-col gap-2">
          <Skeleton class="h-5 w-48" />
          <Skeleton class="h-20 w-full" />
        </div>

        <Alert v-else-if="detailError !== ''" variant="destructive">
          <AlertTitle>无法读取文档</AlertTitle>
          <AlertDescription>{{ detailError }}</AlertDescription>
        </Alert>

        <dl v-else-if="detailDocument !== null" class="grid grid-cols-[5rem_1fr] gap-y-2 text-sm">
          <dt class="text-muted-foreground">标题</dt>
          <dd class="break-words">{{ detailDocument.title }}</dd>
          <dt class="text-muted-foreground">类型</dt>
          <dd>{{ sourceTypeLabel(detailDocument.sourceType) }}</dd>
          <dt class="text-muted-foreground">状态</dt>
          <dd>
            <Badge :variant="lifecycleBadgeVariant(detailDocument.lifecycleStatus)">
              {{ lifecycleLabel(detailDocument.lifecycleStatus) }}
            </Badge>
          </dd>
          <dt class="text-muted-foreground">版本</dt>
          <dd>{{ versionLine(detailDocument) }}</dd>
          <dt class="text-muted-foreground">最新任务</dt>
          <dd>{{ pendingWorkLabel(detailDocument) }}</dd>
          <dt class="text-muted-foreground">创建于</dt>
          <dd>{{ formatTime(detailDocument.createdAt) }}</dd>
          <dt class="text-muted-foreground">更新于</dt>
          <dd>{{ formatTime(detailDocument.updatedAt) }}</dd>
          <dt class="text-muted-foreground">文档 ID</dt>
          <dd class="font-mono text-xs break-all">{{ detailDocument.id }}</dd>
        </dl>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="deleteOpen">
      <DialogContent class="sm:max-w-sm [--destructive:#dc2626]">
        <DialogHeader>
          <DialogTitle>删除文档</DialogTitle>
          <DialogDescription>
            将删除「{{ deleteTarget?.title ?? "" }}」：该文档的检索与引用立即失效，操作不可撤销。
          </DialogDescription>
        </DialogHeader>

        <Alert v-if="deleteError !== ''" variant="destructive">
          <AlertTitle>删除失败</AlertTitle>
          <AlertDescription>{{ deleteError }}</AlertDescription>
        </Alert>

        <DialogFooter>
          <Button variant="outline" type="button" @click="deleteOpen = false">取消</Button>
          <Button variant="destructive" type="button" :disabled="deletePending" @click="confirmDelete">
            {{ deletePending ? "删除中…" : "确认删除" }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>
