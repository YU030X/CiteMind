import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import test from "node:test";

import { citationIdMap, renderMarkdown } from "../src/lib/markdown.ts";

/**
 * 问答复核：只有服务端提供的「标号 → UUID」映射里的 `[n]` 才渲染成可点击按钮；
 * 未知编号、代码区与链接语法保持原样，原始 HTML 与危险协议仍被拒绝。
 * 运行：`node --test tests/markdown.test.ts`（Node 原生类型剥离，不引测试框架）。
 */

test("合法 [n] 渲染为带 UUID 的可点击按钮", () => {
  const map = citationIdMap([
    { displayLabel: "1", citationId: "uuid-1" },
    { displayLabel: "2", citationId: "uuid-2" },
  ]);

  // 服务端为多来源句用空格分隔标号，避免 `[2][1]` 被当成引用式链接语法。
  const html = renderMarkdown("第一句[1]。\n第二句[2] [1]。", map);

  assert.match(html, /data-citation-id="uuid-1">\[1\]<\/button>/);
  assert.match(html, /data-citation-id="uuid-2">\[2\]<\/button>/);
});

test("历史刷新：仅有引用列表也能重建映射并渲染标记", () => {
  const map = citationIdMap([{ displayLabel: "1", citationId: "uuid-history" }]);

  const html = renderMarkdown("旧回答制度规定。[1]", map);

  assert.match(html, /data-citation-id="uuid-history">\[1\]<\/button>/);
});

test("未知编号与不在映射里的编号保持原样文本", () => {
  const html = renderMarkdown("未知[9]与跨消息[1]。", citationIdMap([]));

  assert.doesNotMatch(html, /<button/);
  assert.match(html, /未知\[9\]与跨消息\[1\]。|未知\[9\]/);
});

test("代码区里的 [1] 不渲染按钮且内容不变", () => {
  const map = citationIdMap([{ displayLabel: "1", citationId: "uuid-1" }]);

  const inlineCode = renderMarkdown("`a[1]` 与 [1]。", map);
  assert.match(inlineCode, /<code>a\[1\]<\/code>/);

  const fenced = renderMarkdown("```\na[1]\n```\n[1]", map);
  assert.ok(fenced.includes("a[1]"));
  assert.equal(fenced.match(/<button/g)?.length, 1);
});

test("服务端转义的 \\[n\\] 渲染为字面文本而非按钮", () => {
  const map = citationIdMap([{ displayLabel: "2", citationId: "uuid-2" }]);

  // 服务端把模型自带的 [n] 转义成 \\[n\\]；前端必须只当字面文本，不能变成引用入口。
  const html = renderMarkdown(String.raw`伪造\[2\]来源。`, map);

  assert.doesNotMatch(html, /<button/);
  assert.ok(html.includes("伪造[2]来源。"));
});

test("[1](url) 仍按链接处理，不当作引用按钮", () => {
  const map = citationIdMap([{ displayLabel: "1", citationId: "uuid-1" }]);

  const html = renderMarkdown("[1](https://example.invalid)。", map);

  assert.ok(html.includes('href="https://example.invalid"'));
  assert.doesNotMatch(html, /data-citation-id/);
});

test("原始 HTML 与危险协议仍被拒绝", () => {
  const html = renderMarkdown("<script>alert(1)</script>\n[x](javascript:alert(1))");

  assert.doesNotMatch(html, /<script/);
  // 危险协议不会被做成链接；正文里保留字面文本不算漏洞。
  assert.doesNotMatch(html, /href="javascript:/);
  assert.doesNotMatch(html, /<a /);
  assert.match(html, /&lt;script&gt;/);
});

// 以下四条使用真实 `parse_answer` 产出的 answer_text 字符串（同一字面量在
// tests/unit/test_answer_schema.py 里由服务端断言），验证服务端转义到前端渲染的端到端结果。

test("P1 复现：服务端转义后正文里的 [9] 不再变成未声明的引用按钮", () => {
  const map = citationIdMap([
    { displayLabel: "1", citationId: "u1" },
    { displayLabel: "9", citationId: "u9" },
  ]);
  const serverOutput = "```X\\[9\\]Y``[1]\nadj\\[9\\]\\[1\\]end[1]\nB[9]";

  const html = renderMarkdown(serverOutput, map);

  // 只有第三句真正声明的 [9] 是可点击引用；前两句自写的 [9] 保持字面文本。
  assert.equal(html.match(/data-citation-id="u9"/g)?.length, 1);
  assert.equal(html.match(/data-citation-id="u1"/g)?.length, 2);
  assert.ok(html.includes("```X[9]Y``"));
  assert.ok(html.includes("adj[9][1]end"));
});

test("服务端输出保真：代码区与链接不被转义破坏", () => {
  const map = citationIdMap([{ displayLabel: "1", citationId: "u1" }]);
  const serverOutput = "见 `代码 [9]` 与 [1](https://example.invalid)。[1]";

  const html = renderMarkdown(serverOutput, map);

  assert.ok(html.includes("<code>代码 [9]</code>"));
  assert.ok(html.includes('href="https://example.invalid"'));
  // 链接里的 [1] 不是引用按钮，只有句末服务端标记是。
  assert.equal(html.match(/data-citation-id="u1"/g)?.length, 1);
});

test("服务端追加的 [1] 不被正文结尾反斜杠吞掉", () => {
  const map = citationIdMap([{ displayLabel: "1", citationId: "u1" }]);
  // 服务端把句末孤立反斜杠补成成对反斜杠后再追加 [1]（见 `_append_citation_markers`）。
  const serverOutput = "path C:\\\\[1]";

  const html = renderMarkdown(serverOutput, map);

  assert.equal(html.match(/data-citation-id="u1"/g)?.length, 1);
  assert.ok(html.includes("path C:\\<button"));
});

test("多句跨块：代码块内 [n] 保真，代码块后的服务端标记可点", () => {
  const map = citationIdMap([
    { displayLabel: "1", citationId: "u1" },
    { displayLabel: "9", citationId: "u9" },
  ]);
  const serverOutput =
    "示例：\n```python\nprint(1)  # [9]\n```\n[1]\n后续说明 \\[1\\]。[1]";

  const html = renderMarkdown(serverOutput, map);

  assert.ok(html.includes("print(1)  # [9]"));
  assert.equal(html.match(/data-citation-id="u9"/g)?.length ?? 0, 0);
  // 代码块后的服务端标记是按钮；第二句自写的 [1] 是字面文本。
  assert.ok(html.includes("后续说明 [1]。<button"));
});

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

// 跨语言回归：不在前端重写一份服务端期望，而是真实调用 `parse_answer` 生成 answer_text，
// 再喂真实 `renderMarkdown`，避免「两边各写同一字面量」式的伪 E2E。
const SERVER_ANSWER_SCRIPT = String.raw`
import json

from rag_backend.generation.answer_schema import parse_answer

cases = ["a\\ ", "a\\\t", "a\\\n", "a\\\\"]
out = {}
for text in cases:
    payload = {"sentences": [{"text": text, "citationIds": ["E1"]}], "insufficientEvidence": False, "followUp": None}
    out[text] = parse_answer(json.dumps(payload, ensure_ascii=False), allowed_citation_ids={"E1"}).answer_text
print(json.dumps(out, ensure_ascii=False))
`;

test("末尾反斜杠回归：真实 parse_answer 输出经真实 renderer 仍可点击", () => {
  const raw = execFileSync("uv", ["run", "python", "-c", SERVER_ANSWER_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  const outputs = JSON.parse(raw) as Record<string, string>;
  const map = citationIdMap([{ displayLabel: "1", citationId: "u1" }]);

  // 反斜杠+空格/制表符/换行与偶数反斜杠四种原文。
  assert.equal(Object.keys(outputs).length, 4);
  for (const [sourceText, serverOutput] of Object.entries(outputs)) {
    const html = renderMarkdown(serverOutput, map);
    assert.equal(
      html.match(/data-citation-id="u1"/g)?.length ?? 0,
      1,
      `${JSON.stringify(sourceText)} -> ${JSON.stringify(serverOutput)} 应渲染 1 个引用按钮`,
    );
  }
});
