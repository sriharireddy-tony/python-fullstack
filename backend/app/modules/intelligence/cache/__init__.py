"""Cache serialisation for the AI layer.

The cache *client* lives in `app/core/cache.py` and the key scheme in
`CacheKey` — both are application-wide. What lives here is the AI layer's own
concern: how a similarity result becomes a JSON payload and back.
"""

from app.modules.intelligence.cache.similar_cache import from_cache, to_cache

__all__ = ["from_cache", "to_cache"]
