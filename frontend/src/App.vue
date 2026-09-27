<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch, type Component } from "vue";
import {
  FileTextIcon,
  LibraryIcon,
  LogOutIcon,
  MenuIcon,
  MessagesSquareIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
} from "@lucide/vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import LoginView from "@/components/LoginView.vue";
import ChatView from "@/views/ChatView.vue";
import DocumentsView from "@/views/DocumentsView.vue";
import KnowledgeBasesView from "@/views/KnowledgeBasesView.vue";
import { bootstrap, disposeWorkspace, logout, state } from "@/state/store";

type PageKey = "chat" | "knowledge-bases" | "documents";

interface AppPage {
  key: PageKey;
  label: string;
  icon: Component;
}

/** AI 问答排在第一项：这是工作台的主入口，知识库与文档管理是它的前置数据准备。 */
const pages: AppPage[] = [
  { key: "chat", label: "AI 问答", icon: MessagesSquareIcon },
  { key: "knowledge-bases", label: "知识库", icon: LibraryIcon },
  { key: "documents", label: "文档管理", icon: FileTextIcon },
];

const activeKey = ref<PageKey>(resolveInitialPage());
const activePage = computed(
  () => pages.find((page) => page.key === activeKey.value) ?? pages[0]!,
);
/** 桌面侧边栏折叠状态；折叠后只保留图标，入口仍可识别。 */
const collapsed = ref(false);
/** 窄屏时复用 Sheet 抽屉承载同一套页面入口。 */
const navOpen = ref(false);
/** AI 问答页要占满主内容高度：让页面自己管理滚动。 */
const isChatPage = computed(() => activePage.value.key === "chat");
const collapseLabel = computed(() => (collapsed.value ? "展开主导航" : "折叠主导航"));
const userName = computed(() => state.session?.user.username ?? "");
const userIsAdmin = computed(() => state.session?.user.isAdmin ?? false);

/** 只做 hash 同步，不引入 router，方便直接链接到某一页。 */
function resolveInitialPage(): PageKey {
  const raw = window.location.hash.replace("#", "");
  return pages.some((page) => page.key === raw) ? (raw as PageKey) : "chat";
}

function selectPage(key: PageKey): void {
  activeKey.value = key;
  navOpen.value = false;
}

/** 知识库卡片点「打开」时把用户带到文档管理页。 */
function selectKnowledgeBasePage(): void {
  selectPage("documents");
}

watch(activeKey, (key) => {
  window.history.replaceState(null, "", `#${key}`);
});

onMounted(() => {
  void bootstrap();
});

onUnmounted(() => {
  disposeWorkspace();
});
</script>

<template>
  <div v-if="state.booting" class="mx-auto flex w-full max-w-7xl flex-col gap-3 px-4 py-10">
    <Skeleton class="h-8 w-48" />
    <Skeleton class="h-24 w-full" />
    <Skeleton class="h-24 w-full" />
  </div>

  <div
    v-else-if="state.bootError !== ''"
    class="mx-auto flex w-full max-w-2xl flex-col gap-3 px-4 py-10"
  >
    <Alert variant="destructive">
      <AlertTitle>无法载入工作台</AlertTitle>
      <AlertDescription>{{ state.bootError }}</AlertDescription>
    </Alert>
    <Button type="button" class="hover:bg-primary-hover" @click="bootstrap">
      重试
    </Button>
  </div>

  <LoginView v-else-if="state.session === null" />

  <div
    v-else
    class="flex"
    :class="isChatPage ? 'h-dvh min-h-0 overflow-hidden lg:flex' : 'min-h-screen lg:flex'"
  >
    <aside
      class="sticky top-0 hidden h-screen shrink-0 flex-col border-r bg-card transition-[width] duration-200 lg:flex"
      :class="collapsed ? 'w-16' : 'w-56'"
    >
      <div
        class="flex shrink-0 border-b px-3"
        :class="
          collapsed
            ? 'flex-col items-center gap-1 py-2'
            : 'h-14 items-center justify-between gap-2'
        "
      >
        <div class="flex min-w-0 items-center gap-2">
          <span
            aria-hidden="true"
            class="grid size-7 shrink-0 place-items-center rounded-md bg-primary text-sm font-semibold text-primary-foreground"
          >
            知
          </span>
          <div v-if="!collapsed" class="flex min-w-0 flex-col">
            <span class="truncate text-sm font-medium">知据 CiteMind</span>
            <span class="truncate text-xs text-muted-foreground">个人资料工作台</span>
          </div>
        </div>

        <Button
          variant="ghost"
          size="icon-sm"
          type="button"
          class="shrink-0"
          aria-controls="primary-nav"
          :aria-expanded="!collapsed"
          :aria-label="collapseLabel"
          :title="collapseLabel"
          @click="collapsed = !collapsed"
        >
          <PanelLeftOpenIcon v-if="collapsed" />
          <PanelLeftCloseIcon v-else />
        </Button>
      </div>

      <nav
        id="primary-nav"
        aria-label="主导航"
        class="flex flex-1 flex-col gap-1 overflow-y-auto p-2"
      >
        <button
          v-for="page in pages"
          :key="page.key"
          type="button"
          class="flex items-center gap-2 rounded-md border px-2 py-2 text-sm transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          :class="[
            collapsed ? 'justify-center' : 'text-left',
            page.key === activeKey
              ? 'border-border bg-secondary font-medium'
              : 'border-transparent text-muted-foreground hover:bg-secondary hover:text-foreground',
          ]"
          :title="collapsed ? page.label : undefined"
          :aria-current="page.key === activeKey ? 'page' : undefined"
          @click="selectPage(page.key)"
        >
          <component :is="page.icon" aria-hidden="true" class="size-4 shrink-0" />
          <span :class="collapsed ? 'sr-only' : 'truncate'">{{ page.label }}</span>
        </button>
      </nav>

      <div
        class="flex shrink-0 flex-col gap-2 border-t px-3 py-3"
        :class="collapsed ? 'items-center px-2' : ''"
      >
        <div v-if="!collapsed" class="flex min-w-0 items-center gap-2">
          <span class="min-w-0 truncate text-xs font-medium" :title="userName">
            {{ userName }}
          </span>
          <Badge v-if="userIsAdmin" variant="outline">管理员</Badge>
        </div>

        <Button
          v-if="collapsed"
          variant="ghost"
          size="icon-sm"
          type="button"
          aria-label="退出登录"
          title="退出登录"
          :disabled="state.loggingOut"
          @click="logout"
        >
          <LogOutIcon />
        </Button>
        <Button
          v-else
          variant="outline"
          size="sm"
          type="button"
          class="w-full"
          :disabled="state.loggingOut"
          @click="logout"
        >
          {{ state.loggingOut ? "退出中…" : "退出登录" }}
        </Button>
      </div>
    </aside>

    <div class="flex min-w-0 flex-1 flex-col" :class="isChatPage ? 'min-h-0' : undefined">
      <header
        class="sticky top-0 z-40 flex items-center gap-2 border-b bg-card px-3 py-2 lg:hidden"
      >
        <Button
          variant="ghost"
          size="icon-sm"
          type="button"
          aria-label="打开主导航"
          aria-controls="mobile-nav"
          :aria-expanded="navOpen"
          @click="navOpen = true"
        >
          <MenuIcon />
        </Button>
        <span
          aria-hidden="true"
          class="grid size-7 shrink-0 place-items-center rounded-md bg-primary text-sm font-semibold text-primary-foreground"
        >
          知
        </span>
        <span class="min-w-0 truncate text-sm font-medium">知据 CiteMind</span>
        <span class="ml-auto truncate text-xs text-muted-foreground">
          {{ activePage.label }}
        </span>
      </header>

      <div
        v-if="state.logoutError !== ''"
        class="mx-auto w-full max-w-7xl px-4 pt-4 sm:px-6"
      >
        <Alert variant="destructive">
          <AlertTitle>退出登录失败</AlertTitle>
          <AlertDescription>
            {{ state.logoutError }}。服务端会话可能仍然有效，请重试。
          </AlertDescription>
        </Alert>
      </div>

      <main
        :class="
          isChatPage
            ? 'flex min-h-0 w-full flex-1 flex-col'
            : 'mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-6 sm:px-6'
        "
      >
        <KnowledgeBasesView
          v-if="activeKey === 'knowledge-bases'"
          @open-documents="selectKnowledgeBasePage"
        />
        <DocumentsView v-else-if="activeKey === 'documents'" />
        <ChatView v-else />
      </main>
    </div>

    <Sheet v-model:open="navOpen">
      <SheetContent id="mobile-nav" side="left" class="w-72">
        <SheetHeader>
          <SheetTitle>知据 CiteMind</SheetTitle>
          <SheetDescription>个人资料工作台</SheetDescription>
        </SheetHeader>
        <nav
          aria-label="主导航"
          class="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-4 pb-4"
        >
          <button
            v-for="page in pages"
            :key="page.key"
            type="button"
            class="flex w-full items-center gap-2 rounded-md border px-2 py-2 text-left text-sm transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
            :class="
              page.key === activeKey
                ? 'border-border bg-secondary font-medium'
                : 'border-transparent text-muted-foreground hover:bg-secondary hover:text-foreground'
            "
            :aria-current="page.key === activeKey ? 'page' : undefined"
            @click="selectPage(page.key)"
          >
            <component :is="page.icon" aria-hidden="true" class="size-4 shrink-0" />
            <span class="truncate">{{ page.label }}</span>
          </button>
        </nav>
        <div class="flex flex-col gap-2 border-t px-4 py-3">
          <div class="flex min-w-0 items-center gap-2">
            <span class="min-w-0 truncate text-xs font-medium">{{ userName }}</span>
            <Badge v-if="userIsAdmin" variant="outline">管理员</Badge>
          </div>
          <Button
            variant="outline"
            size="sm"
            type="button"
            class="w-full"
            :disabled="state.loggingOut"
            @click="logout"
          >
            {{ state.loggingOut ? "退出中…" : "退出登录" }}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  </div>
</template>
