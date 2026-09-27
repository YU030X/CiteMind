<script setup lang="ts">
import { ref } from "vue";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { login, state } from "@/state/store";

const username = ref("");
const password = ref("");

async function submit(): Promise<void> {
  if (state.loggingIn) return;
  const ok = await login(username.value, password.value);
  // 只在成功后才清空密码字段；密码不写入任何持久化存储。
  if (ok) password.value = "";
}
</script>

<template>
  <main class="flex min-h-dvh items-center justify-center px-4 py-10">
    <Card class="w-full max-w-sm gap-4 py-6">
      <CardHeader>
        <CardTitle class="text-base">登录知据 CiteMind</CardTitle>
        <CardDescription>
          密码只在本次请求中使用；登录状态由服务端 HttpOnly Cookie 维护，浏览器不保存密码或会话令牌。
        </CardDescription>
      </CardHeader>

      <CardContent class="flex flex-col gap-4">
        <Alert v-if="state.sessionExpiredNotice !== ''">
          <AlertTitle>登录状态已失效</AlertTitle>
          <AlertDescription>{{ state.sessionExpiredNotice }}</AlertDescription>
        </Alert>

        <form class="flex flex-col gap-4" @submit.prevent="submit">
          <div class="flex flex-col gap-2">
            <Label for="login-username">用户名</Label>
            <Input
              id="login-username"
              v-model="username"
              name="username"
              autocomplete="username"
              required
              :disabled="state.loggingIn"
            />
          </div>

          <div class="flex flex-col gap-2">
            <Label for="login-password">密码</Label>
            <Input
              id="login-password"
              v-model="password"
              name="password"
              type="password"
              autocomplete="current-password"
              required
              :disabled="state.loggingIn"
            />
          </div>

          <Alert v-if="state.loginError !== ''" variant="destructive">
            <AlertTitle>登录失败</AlertTitle>
            <AlertDescription>{{ state.loginError }}</AlertDescription>
          </Alert>

          <Button
            type="submit"
            class="w-full hover:bg-primary-hover"
            :disabled="state.loggingIn || username === '' || password === ''"
          >
            {{ state.loggingIn ? "登录中…" : "登录" }}
          </Button>
        </form>
      </CardContent>
    </Card>
  </main>
</template>
