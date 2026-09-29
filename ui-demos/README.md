# CiteMind 界面视觉演示（ui-demos）

> 本目录的视觉与组件源码已经迁入正式 `frontend/`（Tailwind v4 + shadcn-vue/reka-ui，同一套 token 与圆角/阴影规则），并接通了真实接口；本目录保留为**视觉审核参考**，不再作为正式前端，也不再更新其演示数据。

用于**视觉审核**的 3 个独立 Demo 页面，组件来自官方 shadcn-vue（reka-ui + Tailwind CSS v4）生成源码。

本目录与 `frontend/**` 完全隔离：独立 `package.json` 与 `package-lock.json`，不在 pnpm workspace 内，不使用根工程的依赖、脚本或构建产物。

> 未连接 CiteMind 后端。页面里的知识库、文档、状态、会话与回答都是自制静态样例，没有真实上传、解析、检索、模型调用或服务端统计。

## 启动（单行命令）

```
cd ui-demos; npm install; npm run dev
```

Vite 打印的地址即为入口（默认 `http://127.0.0.1:5173/`）。只做构建检查可运行 `cd ui-demos; npm run build`。

## 三个页面入口

页面在左侧主导航（AI 问答 / 知识库 / 文档管理）切换，没有使用 router；不带 hash 时默认打开 AI 问答，有效 hash 直接定位到对应页面：

- 左侧栏可折叠：展开时显示品牌与图标 + 文字，折叠后收成窄图标栏，入口用图标与 `title` 提示保留可识别性，折叠按钮带 `aria-label` 与 `aria-expanded`
- 窄屏（`lg` 以下）不保留常驻侧栏，改用顶部入口按钮 + Sheet 抽屉承载同一套导航

| 页面 | 直接入口 | 内容 |
| --- | --- | --- |
| AI 问答 | `http://127.0.0.1:5173/#chat`（默认页） | 独立问答页面：`xl` 以上左侧固定会话历史（顶部新建对话、置顶分组 + 按今天/昨天/更早分组、可切换、每行省略号菜单可置顶/改名/删除会话），中间问答主体，回答里的 `[1]` 按需用右侧 Sheet 打开引用；更窄时历史收进抽屉（Sheet），避免与主导航争抢宽度 |
| 知识库 | `http://127.0.0.1:5173/#knowledge-bases` | 卡片视图、按名称/描述搜索、新建知识库弹窗（本地模拟，不写任何数据）、卡片操作菜单 |
| 文档管理 | `http://127.0.0.1:5173/#documents` | 紧凑表格、状态与类型筛选、标题搜索、上传文档弹窗（模拟，不会真的上传）；窄屏隐藏次要列 |

### AI 问答页（`#chat`）

- 页面占满主内容高度：全局说明条在问答页隐藏，滚动只发生在消息区，输入框用 flex 固定在主内容底部（不是 `fixed`，不会盖住全局导航）；消息区与输入区之间不画整宽分隔线，只保留输入框自身的细边框
- 全局导航 + 会话历史 + 问答主体三层；历史是页面自带的固定侧栏，不是卡片。`xl` 以下历史进入左侧 Sheet 抽屉
- 会话标题与行尾省略号共用同一行级 hover/选中底色与圆角，两个按钮自身不画背景，hover 行内任一处背景连贯；点菜单不会误切会话（触发器是行按钮的兄弟节点）
- 历史每行省略号菜单（官方 `DropdownMenu`）含置顶/取消置顶、修改标题与红色「删除会话」（置顶区与危险区之间有分隔线）；置顶项进入列表最上方的独立「置顶」分组，其余仍按今天/昨天/更早分组，组内顺序稳定。置顶/取消置顶只切本地标记，切换会话与新建对话都保留，删除置顶项不残留。修改标题用 `Dialog` + `Input` 预填当前标题，非空才可保存（最长 40 字符），取消不改动。删除前用 `Dialog` 二次确认，确认后只从本地列表和消息线程里移除，删掉当前会话时退到列表里的第一个会话、列表空了就回到空欢迎态；等待回答的会话被删除时定时器会被清掉，不会把会话写回来
- 新对话时主区域居中显示欢迎标题与大型输入框；有消息后变成标准消息流。欢迎态与底部是同一份输入状态，交互一致
- 输入区底部工具条在欢迎态与消息态共用：左边是本页的演示说明（窄屏隐藏），右侧是模型 Select、思考程度 Select 与发送按钮
- 模型默认是当前实际使用的 `deepseek-flash`，另有明确标注演示的备选项（`演示模型 A/B`）；思考程度为关闭/低/中/高，默认关闭。两项都只是本地界面状态，不请求接口，也不宣称已支持真实推理参数；切换选择会即时更新本地状态，并作为快照写进之后生成的本地模拟回答，避免历史标记混淆；生成回答期间两个选择器禁用
- 用户消息是浅灰小气泡、右对齐；助手消息无气泡，正文按 Markdown 渲染，代码块为等宽字体加细边框，`[1]` 类是可用键盘聚焦的引用按钮
- 引用永远用右侧 Sheet 按需展示（文档名、版本、行页、chunk 原文），关闭后问答区恢复完整宽度；没有常驻引用列
- 发送后 600ms 由 `src/demos/data.ts` 里的写死文本生成“本地模拟回答”，生成期间禁用发送按钮防重复；不做流式、不发请求
- Markdown 由 `markdown-it` 渲染（`html: false`）：正文里的原始 HTML 只按文本转义，链接过滤协议并统一带 `target="_blank"` 与 `rel="noopener noreferrer nofollow"`；引用标只在本条回答引用的、且本地映射里存在的 ID 内可点击

## 视觉合同

- 页面 `#FAFAFA`、卡片 `#FFFFFF`、次级面 `#F7F7F6`
- 主文 `#18181B`、次文 `#71717A`、细边框 `#E4E4E7`
- 唯一强调色 `#2563EB`，hover `#1D4ED8`；只有危险操作例外：会话菜单里的「删除会话」与删除确认按钮用红色 `#DC2626`（通过局部 `--destructive` 覆盖实现，不改全局 token）
- 圆角：控件 6px、卡片 8px、弹窗 10px
- 无渐变、毛玻璃、重阴影与大圆角；图标为 lucide 细线图标，不做夸张处理
- 中文界面，品牌沿用 CiteMind；不引入外部字体或 CDN，字体使用系统中文栈

配色、圆角与阴影统一在 `src/style.css` 里定义（`--radius: 0.5rem` 让控件落在 6px，卡片/弹窗圆角由未分层的 `[data-slot=...]` 规则收口），**没有改动任何 shadcn-vue 生成的组件源码**。

## 依赖与组件

- `shadcn-vue@2.8.2` CLI 通过官方 registry 初始化（style `reka-vega`、base color `neutral`、icon library `lucide`、CSS variables 开启），配置见 `components.json`
- 按需添加的官方组件：`button`、`input`、`textarea`、`card`、`badge`、`dialog`、`sheet`、`select`、`table`、`separator`、`label`、`tabs`、`scroll-area`、`skeleton`、`alert`、`dropdown-menu`
- 运行时依赖：`vue`、`reka-ui`、`@lucide/vue`、`@vueuse/core`、`markdown-it`、`class-variance-authority`、`clsx`、`tailwind-merge`；样式依赖 `tailwindcss` v4 与 `@tailwindcss/vite`
- Markdown 没有自造解析器，直接用 `markdown-it`；未引入语法高亮（代码块只要清晰等宽样式）
- 未使用 Element Plus 或任何自制同名组件
- 其中 `tabs` 与 `skeleton` 已生成但未接到三个页面上（留给后续扩展），其余 14 个都在 Demo 里实际使用

> 本次生成时本机到 `shadcn-vue.com` 的直连被网络环境阻断，脚本改用本机 HTTP 代理拉取官方 registry，生成的组件源码与依赖版本均来自官方 registry，未手工改写。

## 演示数据的边界

- 只展示演示范围内的两种格式：Markdown 与 PDF（服务端另已支持 DOCX，但不在本静态演示内）；不出现 OCR、重新索引等未实现能力
- 统计数字（文档数、条数）全部是写死的样例文字，页面上明确标注“静态样例，不是服务端统计”
- 所有写操作（新建知识库、上传文档、新建对话、发送消息、删除）只改本地状态并给出“演示：未调用后端接口”的提示；问答页的输入框下方常驻同样说明
- 页头与各页反馈区都声明未连接后端，避免被误读为真实服务行为

## 目录结构

```
ui-demos/
  components.json          # shadcn-vue 配置
  index.html
  package.json             # 独立依赖与脚本（packageManager: npm）
  vite.config.ts           # vue + @tailwindcss/vite + @ 别名
  src/
    App.vue                # 可折叠左侧主导航（窄屏 Sheet）、页面切换、全局演示说明（问答页占满高度）
    style.css              # Tailwind 入口 + 视觉合同变量
    main.ts
    lib/utils.ts           # shadcn-vue 的 cn()
    lib/markdown.ts        # markdown-it 渲染、引用标记与链接安全属性
    components/ui/**       # shadcn-vue 生成的官方组件源码
    demos/
      data.ts              # 静态演示数据、本地模拟回答、历史时间分组
      KnowledgeBaseDemo.vue
      DocumentDemo.vue
      ChatDemo.vue         # AI 问答页（固定侧栏会话历史 + 消息流 + 底部输入 + 引用 Sheet）
      ConversationList.vue # 会话历史（按时间分组、每行省略号删除菜单，问答页侧栏与窄屏 Sheet 共用）
      CitationPanel.vue    # 引用详情（只在右侧 Sheet 里显示）
```
