---
name: git-workflow
description: >
  Apply repository-aware Git conventions for commits, branches, pull requests,
  validation, and release tags. Use when preparing or reviewing a commit,
  choosing a commit type or scope, naming a branch, splitting changes into
  commits, drafting a pull request, checking staged files, or deciding which
  pre-commit commands to run. Discover each repository's actual instructions,
  CI, scripts, and history instead of assuming project-specific commands.
---

# Git 工作流

把提交历史整理成可以快速回答两件事的记录：改了什么、为什么改。验证命令与执行记录属于交付说明或所属验收文档，不写进提交信息。此 skill 提供通用默认规则；仓库自己的 `AGENTS.md`、`CONTRIBUTING.md`、CI 配置和稳定提交历史优先。

## 先发现仓库规则

执行 Git 操作前：

1. 阅读当前目录及上级生效的 `AGENTS.md`、`CLAUDE.md`、`CONTRIBUTING.md` 和仓库文档。
2. 检查当前分支、工作树状态、最近提交信息和暂存区，不覆盖用户已有改动。
3. 从包脚本、构建配置和 CI 工作流中找到实际验证命令；不要复制其他项目的命令。
4. 若文档、CI 和历史互相冲突，指出冲突并询问用户，不要静默选择。

只报告实际执行过的命令和结果。不存在的脚本、测试、构建或发布入口不得写成已验证能力。

## 提交信息

默认格式：

```text
<type>(<scope>): <subject>

<body>

<footer>
```

规则：

- `type` 必填，使用小写英文。
- `scope` 可选，使用小写英文；只有范围明确且能提高检索性时才使用。
- `subject` 必填，使用简体中文、动作描述，不加句号。
- 简单改动优先单行提交；安全、兼容性、性能或复杂取舍需要正文。
- 正文只在需要时说明修改原因、行为影响或兼容限制，不逐行复述 diff。
- 正文不写测试数量、耗时、测试输出，也不写「未推送」「未部署」等执行记录；这些属于交付说明或所属验收文档。
- footer 只用于 Issue、破坏性变更和 `Co-authored-by` 等标准尾部。

默认类型：

| 类型 | 使用场景 |
| --- | --- |
| `feat` | 新增用户可见功能 |
| `fix` | 修复错误或不符合预期的行为 |
| `style` | 视觉样式、间距、排版、颜色或动画，不改变功能意图 |
| `ui` | 页面结构、交互布局、导航、卡片或界面组织方式 |
| `docs` | README、贡献指南或其他文档 |
| `chore` | 不直接改变用户功能的维护性工作 |
| `chore(deps)` | 依赖或锁文件更新 |
| `ci` | CI 工作流、触发条件或检查入口调整 |
| `test` | 测试、fixture 或测试样本调整 |
| `refactor` | 不改变行为的代码重构 |
| `perf` | 加载、构建或运行性能优化 |
| `a11y` | 无障碍改进 |
| `seo` | 搜索可发现性或页面元数据改进 |
| `blog` | 文章内容或博客能力调整 |

优先使用仓库已有类型。现有类型无法准确描述改动时，先讨论，不自行创造新类型。

### Subject 示例

推荐：

```text
fix: 阻止无效封面 URL
docs: 说明语言区域限制
perf: 避免加载已禁用评论
chore(deps): 升级前端依赖
ci: 拆分后端与推理检查
test: 补充迁移降级用例
```

避免笼统描述、过去时、句号或一次罗列多个无关目的，例如 `update stuff`、`Fixed a bug.`、`feat: add search, comments and likes`。

## 提交粒度

一个提交只解决一个清晰目的。不同目的的功能、修复、依赖升级、样例内容和纯格式调整应尽量分开；不要混入无关格式化、重命名、生成物、密钥或临时文件。

同一目的下的实现代码与必要测试、样本、契约、文档同步属于同一提交范围，不必机械拆分，例如解析器版本变更时同步 `manifest`/`dev-questions` 的 `parserVersion`。与目的无关的样本扩充、独立评估归档或后续能力应另开提交。

提交前检查：

1. 查看工作树和暂存区，识别用户原有改动与本次改动。
2. 只按显式路径暂存本次相关文件，不使用全量 `git add -A`/`git add .`，也不因工作树有其他改动就清理或重置。
3. 用 `git diff --check` 和 `git diff --cached` 核对暂存内容与 subject 完全对应。
4. 检查生成文件、凭据、本地环境文件和大文件是否误入；不提交凭据、密钥、生成物或临时文件。
5. 运行与改动相关的最小聚焦检查，再运行仓库规定的提交前门禁；只报告实际运行的检查与结果，不把未运行的检查写成已通过。
6. 若检查失败，保留失败状态并说明位置、原因和未完成项，不提交为成功结果。

提交信息格式是仓库约定，不依赖 hook 或 commitlint 强制，也不为此新增脚本或门禁。除非用户明确要求，不执行 `git commit`、`push`、`tag`、rebase 或破坏性 Git 命令。

## 分支命名

先遵守仓库分支策略。没有明确规则时，推荐小写英文和短横线，并使用与主要提交类型一致的前缀：

```text
feat/<topic>
fix/<topic>
style/<topic>
chore/<topic>
```

例如：

```text
feat/footnote-previews
fix/remote-cover-validation
chore/dependency-upgrades
```

不要未经用户同意创建、切换或删除分支。在默认分支上准备代码改动时，若仓库禁止直接开发，应先提示用户。

## 发现验证命令

验证命令必须来自当前仓库，而不是本模板。按以下顺序发现：

1. 仓库级说明文件与开发文档；
2. CI 工作流；
3. `package.json`、`pyproject.toml`、Makefile、任务运行器或同类配置；
4. 最近成功提交或 Pull Request 中稳定使用的命令。

候选检查通常包括空白错误、静态检查、类型检查、聚焦测试和受影响构建，但不是默认已执行的门禁；只运行当前仓库实际存在的入口并报告实际结果。需要管理员权限、交互式操作或长运行服务时，将单行命令交给用户执行。

## Pull Request

Pull Request 应说明：

- 面向用户或维护者的问题与改动原因；
- 主要实现取舍和明确未覆盖范围；
- 实际执行的验证命令及结果；
- 剩余风险、未运行检查和后续工作；
- 视觉改动的截图或无法提供截图的原因。

标题遵循仓库规则；没有专门规则时可复用提交信息格式。不要声称未运行的 CI、测试或视觉检查已通过。

## 版本标签

先确认仓库的发布流程和版本方案。采用语义化版本时，正式标签默认使用 `v` 前缀，例如 `v1.0.0`。只有默认分支已通过仓库要求的门禁、发布内容已经确认且用户明确要求时，才创建或推送标签。

## 输出要求

准备提交或 Pull Request 时，向用户给出：

1. 建议的提交拆分；
2. 每个提交的完整 message；
3. 已执行验证及结果；
4. 未执行验证与原因；
5. 当前分支、暂存状态和需要用户确认的 Git 操作。
