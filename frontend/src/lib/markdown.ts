import MarkdownIt from "markdown-it";
import type { Env, StateInline } from "markdown-it";

/**
 * 问答页的 Markdown 渲染（markdown-it，`html: false`）。
 *
 * - 原始 HTML 关闭：正文里的标签只会按文本转义输出，不会被执行。
 * - 链接沿用 markdown-it 自带的协议白名单（`javascript:`、`vbscript:`、`file:`、非图片 `data:`
 *   都会被拒绝），并统一补上外链安全属性。
 * - `[1]` 形式的引用标记只在本条回答的「标记 → 引用 ID」映射里存在时才渲染成可点击按钮，
 *   其余情况保持原样文本，正文无法凭自己造出引用入口。
 */
const citationPattern = /^\[(\d{1,3})\]/;

const md = new MarkdownIt({ html: false, linkify: false, breaks: true });

/** 渲染环境里携带的映射，由调用方按「本地引用映射 ∩ 本条回答引用」构造。 */
type CitationEnv = Env & { citationIds?: ReadonlyMap<string, string> };

function citationIdsFrom(env: Env | undefined): ReadonlyMap<string, string> | undefined {
  const value = env?.citationIds;
  return value instanceof Map ? (value as ReadonlyMap<string, string>) : undefined;
}

md.inline.ruler.before("link", "citation", (state: StateInline, silent: boolean) => {
  if (state.src.charCodeAt(state.pos) !== 0x5b /* [ */) return false;

  const matched = citationPattern.exec(state.src.slice(state.pos));
  if (matched === null) return false;

  // `[1](url)`、`[1][ref]`、`[1]:` 仍然交给 markdown-it 自己的链接规则。
  const next = state.src.charAt(state.pos + matched[0].length);
  if (next === "(" || next === "[" || next === ":") return false;

  const marker = matched[1]!;
  const citationIds = citationIdsFrom(state.env);
  if (citationIds === undefined || !citationIds.has(marker)) return false;

  if (!silent) {
    const token = state.push("citation", "", 0);
    token.meta = { id: citationIds.get(marker), marker };
  }
  state.pos += matched[0].length;
  return true;
});

md.renderer.rules.citation = (tokens, idx) => {
  const meta = tokens[idx]!.meta ?? {};
  const id = String(meta.id ?? "");
  const marker = String(meta.marker ?? "");
  return `<button type="button" class="md-citation" data-citation-id="${md.utils.escapeHtml(id)}">[${md.utils.escapeHtml(marker)}]</button>`;
};

md.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
  const token = tokens[idx]!;
  token.attrSet("target", "_blank");
  token.attrSet("rel", "noopener noreferrer nofollow");
  return self.renderToken(tokens, idx, options);
};

/**
 * 由一条消息的引用列表构造「标号 → 引用 ID」映射。
 *
 * 标号取自服务端持久化的 `displayLabel`（正文 `[n]` 里的 n），ID 是对应 citation 的 UUID。
 * 只有这条消息确实带有的引用才会进入映射，正文无法凭自己造出引用入口。
 */
export function citationIdMap(
  citations: readonly { displayLabel: string; citationId: string }[],
): ReadonlyMap<string, string> {
  const markers = new Map<string, string>();
  for (const citation of citations) {
    markers.set(citation.displayLabel, citation.citationId);
  }
  return markers;
}

/**
 * 渲染一段助手回答。
 *
 * @param source Markdown 原文。
 * @param citationIds 本条回答允许点击的「标记 → 引用 ID」映射。
 */
export function renderMarkdown(
  source: string,
  citationIds: ReadonlyMap<string, string> = new Map(),
): string {
  const env: CitationEnv = { citationIds };
  return md.render(source, env);
}
