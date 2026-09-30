import assert from "node:assert/strict";
import module from "node:module";
import test from "node:test";

/**
 * `store.ask` 的当轮拒答/降级提示行为验证。
 *
 * node 的类型剥离要求相对导入带扩展名，而 `store.ts` 的依赖省略了扩展名，
 * 所以用官方内置的同步解析钩子补齐 `.ts`；不引入依赖，也不改动源码导入风格。
 */
module.registerHooks({
  resolve(specifier, context, nextResolve) {
    const fromProject =
      context.parentURL !== undefined && !context.parentURL.includes("node_modules");
    if (
      fromProject &&
      (specifier.startsWith("./") || specifier.startsWith("../")) &&
      !/\.[a-z]+$/.test(specifier)
    ) {
      return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
  },
});

const { answerNoticeFor, ask, openConversation, state } = await import(
  "../src/state/store.ts"
);

type FetchStub = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function answerResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    conversationId: "conv-a",
    messageId: "m1",
    queryRunId: "qr1",
    answer: "回答正文",
    citations: [],
    insufficientEvidence: false,
    degradedStages: [],
    followUp: null,
    usage: {
      localInputTokens: null,
      inputTokenBudget: 4000,
      outputTokenBudget: 800,
      providerPromptTokens: null,
      providerCompletionTokens: null,
    },
    ...overrides,
  };
}

function assistantMessage(messageId: string): Record<string, unknown> {
  return {
    messageId,
    role: "assistant",
    content: "回答正文",
    queryRunId: "qr1",
    createdAt: "2026-09-30T00:00:00Z",
    citations: [],
  };
}

/** 只拦截问答页会发的三类请求；其余路径直接显式失败，避免测试静默漏请求。 */
function stubFetch(handlers: {
  answer?: () => Promise<Response> | Response;
  messages?: () => Promise<Response> | Response;
}): void {
  const stub: FetchStub = async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "POST" && url.endsWith("/messages")) {
      return handlers.answer === undefined ? json(answerResponse()) : handlers.answer();
    }
    if (method === "GET" && url.endsWith("/messages")) {
      return handlers.messages === undefined
        ? json({ conversationId: "conv-a", messages: [] })
        : handlers.messages();
    }
    if (method === "GET" && url.endsWith("/conversations")) {
      return json({ conversations: [] });
    }
    throw new Error(`unexpected fetch ${method} ${url}`);
  };
  globalThis.fetch = stub;
}

function resetState(): void {
  state.session = {
    user: { id: "u1", username: "u", isAdmin: false, organizationId: "o1" },
    csrfToken: "csrf",
  };
  state.generation = {
    enabled: true,
    defaultModel: "deepseek-flash",
    defaultThinking: "disabled",
    models: [
      {
        id: "deepseek-flash",
        thinking: { supported: true, efforts: ["low", "high", "max"], defaultEffort: "high" },
      },
    ],
  };
  state.generationModel = "deepseek-flash";
  state.generationThinking = "disabled";
  state.generationEffort = "high";
  state.activeKbId = "kb-1";
  state.activeConversationId = "conv-a";
  state.messages = [];
  state.messagesError = "";
  state.asking = false;
  state.askError = "";
  state.lastFollowUp = "";
  state.answerNotice = null;
  state.conversations = [];
}

test("ask 消费拒答与降级字段并只绑定到该条回答", async () => {
  resetState();
  stubFetch({
    answer: () =>
      json(
        answerResponse({
          insufficientEvidence: true,
          degradedStages: ["source_retry", "rerank_unavailable"],
        }),
      ),
    messages: () => json({ conversationId: "conv-a", messages: [assistantMessage("m1")] }),
  });

  assert.equal(await ask("年假多少天？"), true);
  const notice = answerNoticeFor("m1");
  assert.ok(notice);
  assert.equal(notice.conversationId, "conv-a");
  assert.equal(notice.insufficientEvidence, true);
  assert.deepEqual(notice.degradedStages, ["source_retry", "rerank_unavailable"]);
  // 同一会话内的其它消息不会继承这轮提示。
  assert.equal(answerNoticeFor("m0"), null);
});

test("正常回答（非拒答且无降级）完全不产生提示", async () => {
  resetState();
  stubFetch({
    answer: () => json(answerResponse()),
    messages: () => json({ conversationId: "conv-a", messages: [assistantMessage("m1")] }),
  });

  assert.equal(await ask("年假多少天？"), true);
  assert.equal(answerNoticeFor("m1"), null);
});

test("晚到的回答不串到提问后切换的新会话", async () => {
  resetState();
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let messageRequests = 0;
  stubFetch({
    answer: async () => {
      await gate;
      return json(answerResponse({ insufficientEvidence: true, followUp: "服务端建议的追问" }));
    },
    messages: () => {
      messageRequests += 1;
      return json({ conversationId: "conv-b", messages: [assistantMessage("m9")] });
    },
  });

  const pending = ask("年假多少天？");
  // 提问仍在途时切到另一个会话。
  await openConversation("conv-b");
  release();
  await pending;

  assert.equal(state.activeConversationId, "conv-b");
  assert.equal(state.answerNotice, null);
  assert.equal(answerNoticeFor("m1"), null);
  assert.equal(answerNoticeFor("m9"), null);
  // 晚到结果不写追问、不报错，也不再为旧会话多发历史请求。
  assert.equal(state.lastFollowUp, "");
  assert.equal(state.askError, "");
  assert.equal(messageRequests, 1);
});

test("晚到的失败不把错误写进已切换的新会话", async () => {
  resetState();
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let messageRequests = 0;
  stubFetch({
    answer: async () => {
      await gate;
      throw new Error("provider failed");
    },
    messages: () => {
      messageRequests += 1;
      return json({ conversationId: "conv-b", messages: [assistantMessage("m9")] });
    },
  });

  const pending = ask("年假多少天？");
  await openConversation("conv-b");
  release();

  assert.equal(await pending, false);
  assert.equal(state.activeConversationId, "conv-b");
  assert.equal(state.askError, "");
  assert.equal(answerNoticeFor("m9"), null);
  assert.equal(messageRequests, 1);
});

test("历史刷新失败时不把提示贴到旧消息，状态保留待对应消息出现", async () => {
  resetState();
  state.messages = [assistantMessage("m0")];
  stubFetch({
    answer: () => json(answerResponse({ degradedStages: ["unsupported_text"] })),
    messages: () => json({ code: "INTERNAL", message: "历史刷新失败" }, 500),
  });

  assert.equal(await ask("年假多少天？"), true);
  // 旧消息不会被误标注。
  assert.equal(answerNoticeFor("m0"), null);
  // 本会话仍活跃，提示保留给实际生成的 m1；消息出现后即可展示。
  assert.equal(state.answerNotice?.messageId, "m1");
  assert.deepEqual(state.answerNotice?.degradedStages, ["unsupported_text"]);
  state.messages = [assistantMessage("m0"), assistantMessage("m1")];
  assert.ok(answerNoticeFor("m1"));
});

test("切换会话清理上一轮提示", async () => {
  resetState();
  stubFetch({
    answer: () => json(answerResponse({ insufficientEvidence: true })),
    messages: () => json({ conversationId: "conv-a", messages: [assistantMessage("m1")] }),
  });

  await ask("年假多少天？");
  assert.ok(answerNoticeFor("m1"));
  await openConversation("conv-b");
  assert.equal(state.answerNotice, null);
  assert.equal(answerNoticeFor("m1"), null);
});
