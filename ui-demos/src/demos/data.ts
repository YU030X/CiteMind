/**
 * 演示用静态数据。
 * 全部为自制样例，未来自任何真实知识库、服务或统计，也未连接后端。
 */

export type DocumentFormat = "markdown" | "pdf";
export type DocumentStatus = "ready" | "queued" | "parsing" | "failed";

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  role: string;
  documentCount: number;
  updatedAt: string;
  formats: DocumentFormat[];
}

export interface DocumentRow {
  id: string;
  title: string;
  format: DocumentFormat;
  status: DocumentStatus;
  version: string;
  sizeLabel: string;
  updatedAt: string;
  uploadedBy: string;
}

export interface Citation {
  /** 服务端式引用 ID，回答正文用 [marker] 指向它。 */
  id: string;
  /** 正文里的引用标记，例如 "1" 对应 [1]。 */
  marker: string;
  documentTitle: string;
  version: string;
  locator: string;
  quote: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: string[];
  followUps?: string[];
  /** 生成这条本地模拟回答时输入区选择项的本地快照，只用于在消息上标明来源。 */
  modelLabel?: string;
  thinkingLabel?: string;
}

export type ConversationGroup = "今天" | "昨天" | "更早";

export interface Conversation {
  id: string;
  title: string;
  scope: string;
  group: ConversationGroup;
  updatedAt: string;
  /** 置顶到列表最上方；只改本地列表状态，不涉及后端。 */
  pinned?: boolean;
}

/** 输入区的思考程度选项：纯本地界面状态，不会映射成任何真实推理参数。 */
export type ThinkingLevel = "off" | "low" | "medium" | "high";

export interface DemoModelOption {
  value: string;
  label: string;
}

/**
 * 输入区模型选择器：默认项是当前实际使用的 DeepSeek Flash，其余是本地演示备选，
 * 不代表任何真实可用的后端型号，也不承诺上下文长度等能力。
 */
export const demoModels: DemoModelOption[] = [
  { value: "deepseek-flash", label: "deepseek-flash" },
  { value: "demo-model-a", label: "演示模型 A" },
  { value: "demo-model-b", label: "演示模型 B" },
];

export const thinkingLevels: Array<{ value: ThinkingLevel; label: string }> = [
  { value: "off", label: "关闭" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

export const formatLabel: Record<DocumentFormat, string> = {
  markdown: "Markdown",
  pdf: "PDF",
};

export const statusLabel: Record<DocumentStatus, string> = {
  ready: "可用",
  queued: "排队中",
  parsing: "解析中",
  failed: "失败",
};

export const statusBadgeVariant: Record<
  DocumentStatus,
  "secondary" | "outline" | "destructive"
> = {
  ready: "secondary",
  queued: "outline",
  parsing: "outline",
  failed: "destructive",
};

export const knowledgeBases: KnowledgeBase[] = [
  {
    id: "kb-handbook",
    name: "员工手册",
    description: "年假、考勤、报销与福利政策，随版本更新保留历史版本。",
    role: "拥有者",
    documentCount: 6,
    updatedAt: "2 小时前",
    formats: ["markdown", "pdf"],
  },
  {
    id: "kb-product",
    name: "产品说明书",
    description: "对外发布的功能说明与常见问题，供客服与售前检索。",
    role: "编辑者",
    documentCount: 14,
    updatedAt: "昨天",
    formats: ["markdown"],
  },
  {
    id: "kb-support",
    name: "客服问答库",
    description: "整理过的工单结论与话术，按主题归档。",
    role: "编辑者",
    documentCount: 9,
    updatedAt: "3 天前",
    formats: ["markdown", "pdf"],
  },
  {
    id: "kb-engineering",
    name: "研发规范",
    description: "代码评审、发布流程与故障复盘模板。",
    role: "读者",
    documentCount: 4,
    updatedAt: "上周",
    formats: ["markdown"],
  },
  {
    id: "kb-contracts",
    name: "合同模板",
    description: "采购与保密协议模板，PDF 扫描件为主。",
    role: "读者",
    documentCount: 3,
    updatedAt: "上周",
    formats: ["pdf"],
  },
];

export const documents: DocumentRow[] = [
  {
    id: "doc-handbook-v3",
    title: "员工手册 v3.md",
    format: "markdown",
    status: "ready",
    version: "v3",
    sizeLabel: "48 KB",
    updatedAt: "2 小时前",
    uploadedBy: "demo-hr",
  },
  {
    id: "doc-cafeteria",
    title: "食堂与班车安排.pdf",
    format: "pdf",
    status: "ready",
    version: "v1",
    sizeLabel: "1.2 MB",
    updatedAt: "昨天",
    uploadedBy: "demo-hr",
  },
  {
    id: "doc-expense",
    title: "报销标准.md",
    format: "markdown",
    status: "parsing",
    version: "v2",
    sizeLabel: "22 KB",
    updatedAt: "12 分钟前",
    uploadedBy: "demo-hr",
  },
  {
    id: "doc-onboarding",
    title: "入职指引.md",
    format: "markdown",
    status: "queued",
    version: "v1",
    sizeLabel: "36 KB",
    updatedAt: "8 分钟前",
    uploadedBy: "demo-hr",
  },
  {
    id: "doc-security",
    title: "信息安全约定.pdf",
    format: "pdf",
    status: "ready",
    version: "v1",
    sizeLabel: "780 KB",
    updatedAt: "4 天前",
    uploadedBy: "demo-staff",
  },
  {
    id: "doc-meeting",
    title: "周会纪要模板.md",
    format: "markdown",
    status: "ready",
    version: "v1",
    sizeLabel: "6 KB",
    updatedAt: "5 天前",
    uploadedBy: "demo-staff",
  },
  {
    id: "doc-benefits",
    title: "补充商业保险说明.pdf",
    format: "pdf",
    status: "failed",
    version: "v1",
    sizeLabel: "2.4 MB",
    updatedAt: "上周",
    uploadedBy: "demo-hr",
  },
];

export const citations: Citation[] = [
  {
    id: "cit-1",
    marker: "1",
    documentTitle: "员工手册 v3.md",
    version: "v3",
    locator: "第 12-18 行",
    quote:
      "正式员工每个自然年享有 12 天带薪年假。入职当年按在职月份比例折算，不足 1 天按 1 天计算。",
  },
  {
    id: "cit-2",
    marker: "2",
    documentTitle: "报销标准.md",
    version: "v2",
    locator: "第 4-9 行",
    quote:
      "市内交通凭票据实报实销；跨城差旅需在出发前提交行程，住宿标准按城市档位执行。",
  },
  {
    id: "cit-3",
    marker: "3",
    documentTitle: "食堂与班车安排.pdf",
    version: "v1",
    locator: "第 3 页",
    quote: "工作日午餐供应时间为 11:30-13:00，班车在下班后 18:15 与 19:15 各发一班。",
  },
  {
    id: "cit-4",
    marker: "4",
    documentTitle: "研发规范 v2.md",
    version: "v2",
    locator: "第 21-27 行",
    quote:
      "发布前必须先在预发环境跑通冒烟检查；配置文件随版本一起入库，回滚以镜像标签为准。",
  },
];

export const conversations: Conversation[] = [
  {
    id: "conv-leave",
    title: "年假与审批流程",
    scope: "员工手册",
    group: "今天",
    updatedAt: "10 分钟前",
  },
  {
    id: "conv-onboarding",
    title: "新人入职要准备什么",
    scope: "产品说明书",
    group: "今天",
    updatedAt: "1 小时前",
  },
  {
    id: "conv-expense",
    title: "差旅报销标准",
    scope: "员工手册",
    group: "昨天",
    updatedAt: "昨天 18:20",
  },
  {
    id: "conv-release",
    title: "发布流程的配置示例",
    scope: "研发规范",
    group: "昨天",
    updatedAt: "昨天 09:40",
  },
  {
    id: "conv-commute",
    title: "班车与午餐安排",
    scope: "员工手册",
    group: "更早",
    updatedAt: "3 天前",
  },
];

export const conversationMessages: Record<string, ChatMessage[]> = {
  "conv-leave": [
    {
      id: "msg-leave-1",
      role: "user",
      content: "正式员工每个自然年有多少天带薪年假？",
    },
    {
      id: "msg-leave-2",
      role: "assistant",
      content:
        "按当前有效的《员工手册 v3》[1]，正式员工每个自然年有 **12 天**带薪年假：\n\n" +
        "- 入职当年按在职月份比例折算，不足 1 天按 1 天计算\n" +
        "- 休假前需要在考勤系统提交申请\n" +
        "- 连续休假超过 5 个工作日时，需要抄送部门负责人\n\n" +
        "历史版本手册里是 10 天，看到 10 天说明引用的是旧版本。",
      citations: ["cit-1"],
      followUps: ["入职当年的年假怎么折算？", "年假需要谁审批？"],
    },
    {
      id: "msg-leave-3",
      role: "user",
      content: "那年假需要谁审批？",
    },
    {
      id: "msg-leave-4",
      role: "assistant",
      content:
        "年假由直属上级审批[1]；连续休假超过 5 个工作日时，需要同时抄送部门负责人[2]。\n\n" +
        "审批通过后额度会在考勤系统中自动扣减。",
      citations: ["cit-1", "cit-2"],
      followUps: ["未使用的年假可以顺延吗？"],
    },
  ],
  "conv-expense": [
    {
      id: "msg-expense-1",
      role: "user",
      content: "跨城差旅的报销标准是什么？",
    },
    {
      id: "msg-expense-2",
      role: "assistant",
      content:
        "按《报销标准.md v2》[2]，差旅费用按下面三条执行：\n\n" +
        "1. 市内交通凭票据实报实销\n" +
        "2. 跨城差旅需要在出发前提交行程\n" +
        "3. 住宿标准按城市档位执行\n\n" +
        "超出标准的部分需要部门负责人单独审批。",
      citations: ["cit-2"],
    },
  ],
  "conv-release": [
    {
      id: "msg-release-1",
      role: "user",
      content: "发布前要做哪些检查？顺便给我一段配置示例。",
    },
    {
      id: "msg-release-2",
      role: "assistant",
      content:
        "《研发规范 v2》[4] 要求先跑通预发冒烟再打标签，配置可以写成这样：\n\n" +
        "```yaml\n" +
        "release:\n" +
        "  smoke: true\n" +
        "  freeze_window: \"18:00-20:00\"\n" +
        "  rollback_tag: citemind-api:v3\n" +
        "```\n\n" +
        "- 冒烟检查未通过时不打标签\n" +
        "- 回滚以镜像标签为准\n" +
        "- 冻结时段内不发布",
      citations: ["cit-4"],
      followUps: ["回滚流程是怎样的？"],
    },
  ],
  "conv-commute": [
    {
      id: "msg-commute-1",
      role: "user",
      content: "班车和午餐分别是什么时间？",
    },
    {
      id: "msg-commute-2",
      role: "assistant",
      content:
        "《食堂与班车安排.pdf》第 3 页[3] 写的是：\n\n" +
        "- 工作日午餐 11:30-13:00\n" +
        "- 班车下班后 18:15、19:15 各发一班",
      citations: ["cit-3"],
    },
  ],
  "conv-onboarding": [],
};

export interface SimulatedAnswer {
  /** 命中任一关键词就使用这条回答，`keywords` 为空表示默认回答。 */
  keywords: string[];
  content: string;
  citations: string[];
}

/**
 * 本地模拟回答：文本写死在这里，不经过检索也不调用模型，
 * 只用于演示 Markdown 渲染、代码块与可点击引用。
 */
const defaultAnswer: SimulatedAnswer = {
  keywords: [],
  content:
    "这是**本地模拟回答**，没有经过检索，也没有调用模型[1]。\n\n" +
    "- 回答文本写死在 `ui-demos/src/demos/data.ts`\n" +
    "- 引用按钮只用来演示交互，不会请求服务端\n" +
    "- 点开引用会显示文档名、版本、定位与 chunk 原文\n\n" +
    "接着问一句，可以看到同一个会话里的消息流怎么继续。",
  citations: ["cit-1"],
};

export const simulatedAnswers: SimulatedAnswer[] = [
  defaultAnswer,
  {
    keywords: ["代码", "示例", "配置", "json", "yaml"],
    content:
      "演示一段带代码块的回答[4]：\n\n" +
      "```json\n" +
      "{\n" +
      "  \"kb_id\": \"kb-handbook\",\n" +
      "  \"top_k\": 5,\n" +
      "  \"cite\": true\n" +
      "}\n" +
      "```\n\n" +
      "- 代码块用等宽字体和细边框展示\n" +
      "- 语法高亮不在本页目标内\n" +
      "- 正文里的 [4] 可以直接点开",
    citations: ["cit-4"],
  },
];

/** 本地挑选一条模拟回答，不涉及任何真实检索。 */
export function pickSimulatedAnswer(question: string): SimulatedAnswer {
  const keyword = question.toLowerCase();
  return (
    simulatedAnswers.find((answer) =>
      answer.keywords.some((item) => keyword.includes(item)),
    ) ?? defaultAnswer
  );
}
