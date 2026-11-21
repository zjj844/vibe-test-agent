"""retrieval —— 供"读需求"使用的精简检索组件。

来源：从 RAG 产品仓库的 `rag/` 移植（chunking / embedding / store / retriever 四个纯算法模块）。
为什么移植而不是依赖：本工具要**独立成一个仓库**，不能因为另一个项目不在就装不起来。

只保留自愈工具真正用到的部分：
- chunk_text     按句切块（需求文档切成子块）
- HashingEmbedder 确定性哈希嵌入（不需要下模型、不需要联网、每次结果一样）
- VectorStore    内存向量库
- Retriever      余弦检索

**没有移植**：RAG 的 pipeline / generator / 评估 / 引用校验 —— 那些是"产品"的事，不是"工具"的事。
"""
from .chunking import Chunk, chunk_text, normalize, split_sentences
from .embedding import HashingEmbedder, grams
from .retriever import Retriever
from .store import VectorStore

__all__ = [
    "Chunk",
    "chunk_text",
    "normalize",
    "split_sentences",
    "HashingEmbedder",
    "grams",
    "Retriever",
    "VectorStore",
]
