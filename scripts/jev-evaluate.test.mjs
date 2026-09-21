import assert from "node:assert/strict";
import test from "node:test";

import { parseRequestText, runEvaluation } from "./jev-evaluate.mjs";

test("接受 state 与非空 questions", () => {
  const request = parseRequestText(
    JSON.stringify({
      state: { change: "新增健康检查" },
      questions: {
        safe: {
          type: "boolean",
          instructions: "该改动是否安全？",
        },
      },
    }),
  );

  assert.equal(request.state.change, "新增健康检查");
  assert.equal(request.questions.safe.type, "boolean");
});

test("拒绝调用方覆盖模型", () => {
  assert.throws(
    () =>
      parseRequestText(
        JSON.stringify({
          model: "other/model",
          state: "状态",
          questions: { safe: { type: "boolean", instructions: "安全吗？" } },
        }),
      ),
    /不支持的字段：model/,
  );
});

test("拒绝空 questions", () => {
  assert.throws(
    () => parseRequestText(JSON.stringify({ state: "状态", questions: {} })),
    /questions 必须是非空对象/,
  );
});

test("拒绝不完整的 choice 问题", () => {
  assert.throws(
    () =>
      parseRequestText(
        JSON.stringify({
          state: "状态",
          questions: {
            route: { type: "choice", instructions: "选择处理方式", criteria: {} },
          },
        }),
      ),
    /choice criteria 必须是非空对象/,
  );
});

test("调用前检查 Gateway 密钥", async () => {
  const previousKey = process.env.AI_GATEWAY_API_KEY;
  delete process.env.AI_GATEWAY_API_KEY;

  try {
    await assert.rejects(
      () =>
        runEvaluation({
          state: "状态",
          questions: {
            safe: { type: "boolean", instructions: "是否安全？" },
          },
        }),
      /缺少 AI_GATEWAY_API_KEY/,
    );
  } finally {
    if (previousKey === undefined) {
      delete process.env.AI_GATEWAY_API_KEY;
    } else {
      process.env.AI_GATEWAY_API_KEY = previousKey;
    }
  }
});
