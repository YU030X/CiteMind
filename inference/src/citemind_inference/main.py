from citemind_inference.app import create_app
from citemind_inference.embeddings import load_embedder

# 真实入口：lifespan 启动时加载冻结 revision 的本地模型，缺失或不符即启动失败。
app = create_app(embedder_factory=load_embedder)
