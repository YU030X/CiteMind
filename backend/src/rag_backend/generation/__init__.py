"""Phase 1 生成切片：问答上下文的本地 token 估算与确定性预算选择。

本包提供两部分能力：

- 冻结的 DeepSeek V4.1 文本 chat 提示渲染（纯函数、不导入 ``tokenizers``）：渲染规则取自 recipe 源码
  ``deepseek-ai/deepseek-recipe`` 的固定 commit（``<｜System｜>``、assistant 轮次自带的
  ``<｜Assistant｜></think>`` 与末尾生成前缀），契约名 ``deepseek-v41-chat-v2``；本地 token 计数器从
  已烘入镜像的固定 tokenizer 产物按大小 + SHA-256 校验后离线加载（tokenizer 来源是 HF 模型仓库的
  另一个独立 revision），计数一律标注为 **本地估算**，不是 provider 精确用量。
- 纯上下文选择：系统提示与当前问题优先保留、历史按最近与相关性裁剪（最多 3 轮）、证据按检索
  顺序加入（最多 6 段、同一文档最多 3 段）；系统提示与当前问题单独超预算时显式返回预算错误，
  单个候选正文含本地结构 token 时只跳过该候选并记 ``unsupported_text``。

本包不调用任何 LLM、不写 ``llm_usage``、不做引用映射、不做会话持久化，也不访问数据库或网络。
"""
