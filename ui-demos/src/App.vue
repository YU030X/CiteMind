<script setup lang="ts">
import { computed, ref, watch, type Component } from "vue";
import {
  FileTextIcon,
  InfoIcon,
  LibraryIcon,
  MenuIcon,
  MessagesSquareIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
} from "@lucide/vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import ChatDemo from "@/demos/ChatDemo.vue";
import DocumentDemo from "@/demos/DocumentDemo.vue";
import KnowledgeBaseDemo from "@/demos/KnowledgeBaseDemo.vue";

type PageKey = "knowledge-bases" | "documents" | "chat";

interface DemoPage {
  key: PageKey;
  label: string;
  icon: Component;
  component: Component;
}

const pages: DemoPage[] = [
  { key: "chat", label: "AI 问答", icon: MessagesSquareIcon, component: ChatDemo },
  {
    key: "knowledge-bases",
    label: "知识库",
    icon: LibraryIcon,
    component: KnowledgeBaseDemo,
  },
  { key: "documents", label: "文档管理", icon: FileTextIcon, component: DocumentDemo },
];

const activeKey = ref<PageKey>(resolveInitialPage());
const activePage = computed(
  () => pages.find((page) => page.key === activeKey.value) ?? pages[0],
);
/** 桌面侧边栏折叠状态；折叠后只保留图标，入口仍可识别。 */
const collapsed = ref(false);
/** 窄屏时复用 Sheet 抽屉承载同一套页面入口。 */
const navOpen = ref(false);
/** AI 问答页要占满主内容高度：隐藏全局说明条，并让页面自己管理滚动。 */
const isChatPage = computed(() => activePage.value.key === "chat");

const collapseLabel = computed(() => (collapsed.value ? "展开主导航" : "折叠主导航"));

/** 只做 hash 同步，不引入 router，方便直接链接到某个演示页面。 */
function resolveInitialPage(): PageKey {
  const raw = window.location.hash.replace("#", "");
  return pages.some((page) => page.key === raw) ? (raw as PageKey) : "chat";
}

function selectPage(key: PageKey) {
  activeKey.value = key;
  navOpen.value = false;
}

watch(activeKey, (key) => {
  window.history.replaceState(null, "", `#${key}`);
});
</script>

<template>
  <div
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
            C
          </span>
          <div v-if="!collapsed" class="flex min-w-0 flex-col">
            <span class="truncate text-sm font-medium">CiteMind</span>
            <span class="truncate text-xs text-muted-foreground">界面视觉演示</span>
          </div>
        </div>

        <Button
          variant="ghost"
          size="icon-sm"
          type="button"
          class="shrink-0"
          aria-controls="demo-primary-nav"
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
        id="demo-primary-nav"
        aria-label="演示页面"
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
          @click="activeKey = page.key"
        >
          <component :is="page.icon" aria-hidden="true" class="size-4 shrink-0" />
          <span :class="collapsed ? 'sr-only' : 'truncate'">{{ page.label }}</span>
        </button>
      </nav>

      <p
        v-if="!collapsed"
        class="border-t px-3 py-3 text-xs leading-5 text-muted-foreground"
      >
        未连接 CiteMind 后端，页面内容为自制静态样例。
      </p>
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
          aria-controls="demo-mobile-nav"
          :aria-expanded="navOpen"
          @click="navOpen = true"
        >
          <MenuIcon />
        </Button>
        <span
          aria-hidden="true"
          class="grid size-7 shrink-0 place-items-center rounded-md bg-primary text-sm font-semibold text-primary-foreground"
        >
          C
        </span>
        <span class="min-w-0 truncate text-sm font-medium">CiteMind</span>
        <span class="ml-auto truncate text-xs text-muted-foreground">
          {{ activePage.label }}
        </span>
      </header>

      <main
        :class="
          isChatPage
            ? 'flex min-h-0 w-full flex-1 flex-col'
            : 'mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-6 sm:px-6'
        "
      >
        <Alert v-if="!isChatPage">
          <InfoIcon />
          <AlertTitle>演示数据说明</AlertTitle>
          <AlertDescription>
            本页只用于视觉审核：所有知识库、文档、状态与回答都是自制静态样例，没有连接
            CiteMind 后端，也没有真实上传、检索或统计。
          </AlertDescription>
        </Alert>

        <component :is="activePage.component" :key="activePage.key" />
      </main>
    </div>

    <Sheet v-model:open="navOpen">
      <SheetContent id="demo-mobile-nav" side="left" class="w-72">
        <SheetHeader>
          <SheetTitle>切换演示页面</SheetTitle>
          <SheetDescription>页面内容为自制静态样例，未连接后端。</SheetDescription>
        </SheetHeader>
        <nav
          aria-label="演示页面"
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
      </SheetContent>
    </Sheet>
  </div>
</template>
