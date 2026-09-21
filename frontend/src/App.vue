<script setup lang="ts">
const foundations = [
  "FastAPI 健康检查与 OpenAPI",
  "Vue 3 + TypeScript 控制台",
  "Jev 开发期判断工作流",
];
</script>

<template>
  <main class="shell">
    <section class="hero" aria-labelledby="page-title">
      <p class="eyebrow">CiteMind · Phase 0</p>
      <h1 id="page-title">知据工程骨架</h1>
      <p class="summary">
        面向可核对来源、版本和权限的企业知识库。当前只提供工程入口，不代表检索、入库或问答链路已经实现。
      </p>

      <ul class="foundation-list">
        <li v-for="foundation in foundations" :key="foundation">
          <span aria-hidden="true">✓</span>
          {{ foundation }}
        </li>
      </ul>
    </section>
  </main>
</template>
