# Agent Note：Phase 3 第 2 片离线排序指标、拒答标定与消融产物契约

- 状态：已实现（纯离线数学与产物 schema）；真实探针未运行
- 范围：`rag_backend.evaluation` 的 `ranking_metrics`、`calibration`、`ablation`、`analysis`

## 背景

Phase 3 第 1 片只固定了 100 题数据契约，没有任何排序指标、拒答标定或消融产物的计算入口。第 2 片
要在**不接触真实检索/问答链路**的前提下，先把 Recall@10/nDCG@10 的确定性定义、拒答阈值扫描和
A/B/C 消融产物的严格 schema 定下来，供下一片真实探针填写数据。

约束：本片不新增生产路由、配置或迁移，不连接数据库/HTTP，不调用模型；不产生任何真实指标数值。

## 决策

1. **相关性是确定性的 locator 相交，不做语义判断。** 候选与 gold span 的 (KB、文档、版本、
   解析器版本、来源类型) 一致且 locator 相交即相关；Markdown 用 1-based 行闭区间相交，PDF 用
   1-based 页号相等。它不等价于“区间并集完整覆盖”，也不等于句子级引用支持率。
2. **每个 gold span 最多贡献一次。** 重复 chunk 命中同一 span 只记一次；二值增益下 nDCG 只在
   更高 rank 候选已消耗该 span 时不再给增益。cross-document 覆盖按 gold span 计数。
3. **固定 @10 与纯离线公式。** `Recall@10 = 覆盖 gold span 数 / gold span 总数`；
   `DCG@10 = Σ rel_i / log2(i+1)`，`IDCG@10` 用 `min(gold span 数, 10)`；无 gold 的拒答题不进入
   排序分母，只留给标定。
4. **拒答阈值只在观察到的有限 score 上扫描。** 预测规则是 `topScore < t` 或 `candidateCount == 0`
   即拒答；阈值集合由观察到的有限 score 加两侧确定性边界构成。空分母返回 `None`，NaN/Inf、负
   `candidateCount`、重复题目 id 直接拒绝。
5. **只用开发集选点。** `select_dev_threshold` 按最高 `balancedAccuracy` 选点并只在开发集调用；
   留出集只能用 `evaluate_refusal_threshold` 报告预先固定的阈值，避免在留出集上偷偷调参。
6. **消融产物是严格 schema，不补默认值、不伪造数据。** `A_VECTOR` 只允许向量路且最终 rank 等于
   `vectorRank`；`B_RRF` 必须有 `fusionRank/Score` 且无 `rerankScore`；`C_RERANK` 必须有融合字段，
   `rerankScore` 可选，声明 `rerank_unavailable` 时必须无重排分且最终顺序与 B 完全相同。
   `validate_ablation_triplet` 只做确定性一致性检查（题目集合、dataset 元数据、每题授权 scope、
   C 降级回退顺序），不判定候选是否真由模型产生。
7. **CLI 另开入口。** 新增 `python -m rag_backend.evaluation.analysis` 读取题集与三个产物，保持
   既有 `python -m rag_backend.evaluation` 语义不变。

## 后果与边界

- 本片只交付离线数学与产物契约；**没有真实探针运行、没有 Recall/nDCG/标定数值**，也没有价格快照
  或成本统计（留待后续片）。
- 文档中原有的“区间并集完整覆盖”“rel=0/1/2 的分级增益”等表述仍是计划目标，与本片已实现的
  binary-gain 相交定义不同；以本 Agent Note 与 `docs/evaluation.md` 的实现段落为准。
- `analysis` 是唯一新增 CLI 入口，纯离线；它不校验候选是否来自真实模型，也不代表真实质量。
