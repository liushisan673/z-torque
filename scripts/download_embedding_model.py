"""Download and warm the pinned CPU-only Chinese Embedding model for offline packaging."""
from pathlib import Path

from fastembed import TextEmbedding

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "models" / "embedding-cache"
MODEL = "BAAI/bge-small-zh-v1.5"


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"Preparing {MODEL} in {CACHE}")
    encoder = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE))
    vectors = list(encoder.embed([
        "Z-Torque 是一个本地 AI Agent 编排与执行平台。",
        "知识库使用中文语义向量检索与全文检索。",
    ]))
    if len(vectors) != 2 or not len(vectors[0]):
        raise RuntimeError("Embedding model warm-up returned invalid vectors")
    if len(vectors[0]) != 512 or len(vectors[1]) != 512:
        raise RuntimeError("Unexpected BGE-small-zh-v1.5 vector dimension")
    print(f"Model ready: {len(vectors)} vectors, dimension={len(vectors[0])}")


if __name__ == "__main__":
    main()
