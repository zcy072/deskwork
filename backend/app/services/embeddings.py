"""文本向量化:调用 OpenAI 兼容的 /embeddings 接口(如硅基流动的 BAAI/bge-m3)。

只在配置了 EMBEDDING_API_KEY 时启用。启用后,被检索文档的段落文本会发给该服务,
所以默认关闭,由使用者自己决定。
"""

import asyncio

import httpx

from app.config import settings

BATCH_SIZE = 32
TIMEOUT_SEC = 60.0
MAX_CONCURRENT_BATCHES = 4


def embedding_enabled() -> bool:
    return bool(settings.EMBEDDING_API_KEY.strip() and settings.EMBEDDING_BASE_URL.strip())


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化,返回与 texts 等长、同序的向量。任何失败直接抛异常,由调用方决定是否降级。"""
    if not texts:
        return []
    url = settings.EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"}
    batches = [texts[i : i + BATCH_SIZE] for i in range(0, len(texts), BATCH_SIZE)]
    sem = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)

    async def one(client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        async with sem:
            resp = await client.post(
                url, headers=headers, json={"model": settings.EMBEDDING_MODEL, "input": batch}
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            if len(data) != len(batch):
                raise RuntimeError(f"embedding 返回 {len(data)} 条,期望 {len(batch)} 条")
            return [d["embedding"] for d in data]

    async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
        results = await asyncio.gather(*(one(client, b) for b in batches))
    return [vec for batch_vecs in results for vec in batch_vecs]


def cosine_with_norms(a: list[float], na: float, b: list[float], nb: float) -> float:
    """范数已预先算好的余弦相似度。"""
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)
