from citemind_inference.app import create_app
from citemind_inference.embeddings import load_embedder
from citemind_inference.reranker import load_reranker

# 真实入口：lifespan 启动时加载冻结 revision 的本地模型，缺失或不符即启动失败。
# reranker 只在 RERANK_ENABLED=1 时加载；默认关闭时不触碰模型目录。
app = create_app(embedder_factory=load_embedder, reranker_factory=load_reranker)
