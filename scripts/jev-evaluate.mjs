import fs from "node:fs";
import { pathToFileURL } from "node:url";

import { experimental_evaluate as evaluate } from "ai";

const MAX_REQUEST_BYTES = 128 * 1024;
const MODEL = "typesafe-ai/jev";

export function parseRequestText(text) {
  if (Buffer.byteLength(text, "utf8") > MAX_REQUEST_BYTES) {
    throw new Error(`请求超过 ${MAX_REQUEST_BYTES} 字节限制`);
  }

  let request;
  try {
    request = JSON.parse(text);
  } catch {
    throw new Error("请求不是有效 JSON");
  }

  if (request === null || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("请求必须是 JSON 对象");
  }

  const allowedKeys = new Set(["state", "questions"]);
  const unexpectedKeys = Object.keys(request).filter((key) => !allowedKeys.has(key));
  if (unexpectedKeys.length > 0) {
    throw new Error(`请求包含不支持的字段：${unexpectedKeys.join(", ")}`);
  }

  if (!("state" in request)) {
    throw new Error("请求缺少 state");
  }

  if (
    request.questions === null ||
    typeof request.questions !== "object" ||
    Array.isArray(request.questions) ||
    Object.keys(request.questions).length === 0
  ) {
    throw new Error("questions 必须是非空对象");
  }

  for (const [id, question] of Object.entries(request.questions)) {
    if (question === null || typeof question !== "object" || Array.isArray(question)) {
      throw new Error(`问题 ${id} 必须是对象`);
    }
    if (!("instructions" in question)) {
      throw new Error(`问题 ${id} 缺少 instructions`);
    }

    if (question.type === "choice") {
      if (
        question.criteria === null ||
        typeof question.criteria !== "object" ||
        Array.isArray(question.criteria) ||
        Object.keys(question.criteria).length === 0
      ) {
        throw new Error(`问题 ${id} 的 choice criteria 必须是非空对象`);
      }
    } else if (question.type === "score") {
      if (!Array.isArray(question.criteria) || question.criteria.length < 2) {
        throw new Error(`问题 ${id} 的 score criteria 至少需要两个等级`);
      }
    } else if (question.type !== "boolean") {
      throw new Error(`问题 ${id} 使用了不支持的类型`);
    }
  }

  return request;
}

async function readRequestText(inputPath) {
  if (inputPath) {
    const size = fs.statSync(inputPath).size;
    if (size > MAX_REQUEST_BYTES) {
      throw new Error(`请求超过 ${MAX_REQUEST_BYTES} 字节限制`);
    }
    return fs.readFileSync(inputPath, "utf8");
  }

  if (process.stdin.isTTY) {
    throw new Error("请提供 JSON 文件路径，或通过标准输入传入 JSON");
  }

  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_REQUEST_BYTES) {
      throw new Error(`请求超过 ${MAX_REQUEST_BYTES} 字节限制`);
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

export async function runEvaluation(request) {
  if (!process.env.AI_GATEWAY_API_KEY) {
    throw new Error("缺少 AI_GATEWAY_API_KEY");
  }

  const result = await evaluate({
    model: MODEL,
    state: request.state,
    questions: request.questions,
  });

  return {
    model: result.response.modelId,
    answers: result.answers,
    usage: result.usage,
    warnings: result.warnings,
  };
}

async function main() {
  const inputPath = process.argv[2];
  const request = parseRequestText(await readRequestText(inputPath));
  const result = await runEvaluation(request);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

const entryUrl = process.argv[1] ? pathToFileURL(process.argv[1]).href : undefined;
if (entryUrl === import.meta.url) {
  main().catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`Jev 调用失败：${message}\n`);
    process.exitCode = 1;
  });
}
