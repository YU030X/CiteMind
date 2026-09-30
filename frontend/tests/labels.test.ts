import assert from "node:assert/strict";
import test from "node:test";

import {
  describeLocator,
  degradedStageLabel,
  jobErrorLabel,
  sourceTypeLabel,
} from "../src/labels.ts";

/** DOCX 定位与类型的展示最小断言：只按 locator_version 解释已知键，未知不猜。 */

test("sourceTypeLabel 与 DOCX 诊断码有中文映射", () => {
  assert.equal(sourceTypeLabel("docx"), "DOCX");
  assert.equal(jobErrorLabel("PIPELINE_DOCX_UNSUPPORTED"), "DOCX 含不支持的结构（如嵌套表格）");
  assert.equal(jobErrorLabel("PIPELINE_DOCX_INVALID"), "DOCX 文件损坏或超出安全上限");
});

test("describeLocator 解释 locator_version=3 的段落与表格行", () => {
  const paragraphs = describeLocator({
    locator_version: 3,
    source_type: "docx",
    segments: [{ paragraph_index: 2 }, { paragraph_index: 4 }],
  });
  assert.deepEqual(paragraphs, { kind: "blocks", text: "第 2 段；第 4 段" });

  const rows = describeLocator({
    locator_version: 3,
    source_type: "docx",
    segments: [{ table_index: 1, row_index: 2 }],
  });
  assert.deepEqual(rows, { kind: "blocks", text: "第 1 个表格第 2 行" });
});

test("describeLocator 对未知版本不猜测", () => {
  const unknown = describeLocator({ locator_version: 99, foo: "bar" });
  assert.equal(unknown.kind, "raw");
});

test("sourceTypeLabel 与网页抓取诊断展示", () => {
  assert.equal(sourceTypeLabel("web"), "网页");
  assert.equal(jobErrorLabel("PIPELINE_CONTENT_EMPTY"), "未提取到正文");
});

test("describeLocator 解释 locator_version=4 的原文 URL 与块 ordinal", () => {
  const view = describeLocator({
    locator_version: 4,
    source_type: "web",
    source_url: "https://example.com/a",
    segments: [{ block_ordinal: 0 }, { block_ordinal: 2 }],
  });
  assert.deepEqual(view, { kind: "blocks", text: "https://example.com/a · 第 0、2 块" });
});

test("degradedStageLabel 映射三个真实降级阶段并对未知码安全回退", () => {
  assert.equal(degradedStageLabel("unsupported_text"), "部分资料片段无法安全处理，已跳过");
  assert.equal(degradedStageLabel("source_retry"), "资料变化，已重新检索");
  assert.equal(
    degradedStageLabel("rerank_unavailable"),
    "语义重排不可用，已按原融合排序继续",
  );
  assert.equal(degradedStageLabel("future_stage"), "降级阶段 future_stage");
});
