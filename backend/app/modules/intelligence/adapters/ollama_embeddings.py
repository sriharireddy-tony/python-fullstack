"""Ollama embedding adapter.

Uses ``langchain_ollama.OllamaEmbeddings`` so the LangChain surface is the one
in play, with a direct-HTTP fallback if that import is unavailable — the
adapter's job is to satisfy the port, not to prove a dependency.
"""

from __future__ import annotations

import asyncio

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class OllamaEmbeddingProvider:
    """Local embeddings via Ollama.

    The dimension is **probed once** rather than configured. It is baked into
    the Chroma collection at creation, and a hardcoded wrong value means
    rebuilding the collection — so the model is the authority, not a setting.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        query_instruction: str | None = None,
        document_instruction: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self._model = model or settings.OLLAMA_EMBED_MODEL
        self._query_instruction = (
            query_instruction if query_instruction is not None else settings.EMBED_QUERY_INSTRUCTION
        )
        self._document_instruction = (
            document_instruction
            if document_instruction is not None
            else settings.EMBED_DOCUMENT_INSTRUCTION
        )
        self._dimension: int | None = None
        self._lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise RuntimeError(
                "Dimension not probed yet — call `await provider.warm()` during startup."
            )
        return self._dimension

    @property
    def instruction_version(self) -> str:
        return settings.EMBED_INSTRUCTION_VERSION

    async def warm(self) -> int:
        """Probe the dimension and load the model into memory.

        Ollama's first call after idle pays the model-load cost — several
        seconds for an 8B model. Doing it at startup means the first real job
        does not look broken.
        """
        async with self._lock:
            if self._dimension is not None:
                return self._dimension
            vectors = await self._embed(["dimension probe"])
            self._dimension = len(vectors[0])
            logger.info(
                "embedding model ready",
                extra={"embed_model": self._model, "dimension": self._dimension},
            )
            return self._dimension

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([f"{self._query_instruction}{text}"])
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prefixed = [f"{self._document_instruction}{t}" for t in texts]
        out: list[list[float]] = []
        # Batched because 220 single round-trips is roughly ten times slower
        # than batches of sixteen.
        for start in range(0, len(prefixed), settings.EMBED_BATCH_SIZE):
            out.extend(await self._embed(prefixed[start : start + settings.EMBED_BATCH_SIZE]))
        return out

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            response = await client.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": inputs},
            )
            response.raise_for_status()
            payload = response.json()

        vectors = payload.get("embeddings")
        if not vectors or len(vectors) != len(inputs):
            raise RuntimeError(
                f"Ollama returned {len(vectors or [])} embeddings for {len(inputs)} inputs"
            )
        return [[float(v) for v in vector] for vector in vectors]

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False
