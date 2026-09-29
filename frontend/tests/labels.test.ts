import assert from "node:assert/strict";
import test from "node:test";

import { describeLocator, jobErrorLabel, sourceTypeLabel } from "../src/labels.ts";

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
