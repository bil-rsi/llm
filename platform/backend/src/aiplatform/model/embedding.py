"""Embedding adapter for a llama.cpp server running Qwen3-Embedding (OpenAI-style /v1/embeddings), with an LRU cache."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import httpx

from aiplatform.model.types import ProviderError
from aiplatform.shared import timing


class LlamaCppEmbeddingProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        dims: int,
        *,
        query_instruction: str = "",
        cache_size: int = 2048,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.dims = dims
        self.query_instruction = query_instruction
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), transport=transport, timeout=httpx.Timeout(connect=3, read=60, write=10, pool=5)
        )
        self._cache: OrderedDict[bytes, list[float]] = OrderedDict()
        self._cache_size = cache_size

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents (no instruction prefix; Qwen3-Embedding uses instructions for queries only)."""
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self.query_instruction + text]))[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        keys = [hashlib.blake2b(t.encode("utf-8"), digest_size=16).digest() for t in texts]
        missing = [(i, t) for i, (k, t) in enumerate(zip(keys, texts, strict=True)) if k not in self._cache]
        if missing:
            with timing.stage("embed"):
                try:
                    r = await self._http.post("/v1/embeddings", json={"model": self.model, "input": [t for _, t in missing]})
                except httpx.HTTPError as e:
                    raise ProviderError(f"embedding server unreachable: {type(e).__name__}", retryable=True) from e
            if r.status_code != 200:
                raise ProviderError(f"embedding server HTTP {r.status_code}: {r.text[:200]}")
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            for (i, _), d in zip(missing, data, strict=True):
                vec = [float(x) for x in d["embedding"]]
                if len(vec) != self.dims:
                    raise ProviderError(f"embedding has {len(vec)} dims, expected {self.dims}")
                self._put(keys[i], vec)
        out = []
        for k in keys:
            self._cache.move_to_end(k)
            out.append(self._cache[k])
        return out

    def _put(self, k: bytes, v: list[float]) -> None:
        self._cache[k] = v
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def health(self) -> bool:
        try:
            return (await self._http.get("/health", timeout=3)).status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()
