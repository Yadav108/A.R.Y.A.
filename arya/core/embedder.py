"""
Semantic embedder — Phase 3 of the ARYA roadmap.

Loads all-MiniLM-L6-v2 once (~90 MB, CPU).
Vectors are L2-normalised at produce time so dot-product == cosine similarity.
"""

import asyncio

import numpy as np

_model = None
DIM = 384
_BLOB_SIZE = DIM * 4  # bytes per stored vector


def load_embedder():
    """Load the model synchronously (call from run_in_executor at startup)."""
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers not installed. Run: pip install sentence-transformers"
        ) from exc

    print("Embedder: loading all-MiniLM-L6-v2 (CPU)...")
    _model = SentenceTransformer("all-MiniLM-L6-v2")
    print("Embedder: model ready.")
    return _model


def embed_sync(text: str) -> np.ndarray:
    """Return a unit-normalised 384-dim float32 vector. Blocks the caller."""
    model = load_embedder()
    vec = model.encode(text, normalize_embeddings=True, convert_to_numpy=True)
    return vec.astype(np.float32)


async def embed(text: str) -> np.ndarray:
    """Non-blocking: runs embedding in the default thread executor (~5 ms after warm-up)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed_sync, text)
