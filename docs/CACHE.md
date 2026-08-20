# Cache Package

The `cache/` package provides a standalone, strictly typed caching solution designed for safety, predictability, and static analysis.

## Architecture

The package is built around an Abstract Base Class (ABC) and a secure factory pattern, deliberately avoiding dynamic imports to ensure maximum IDE support, static type checking, and runtime security.

## Core Components

### `CacheBackend` ABC

The `CacheBackend` defines the typed contract for all cache implementations. 

**TTL Contract**
The Time-To-Live (TTL) semantics are strictly pinned to the following behavior:
- `ttl=None`: The item **never expires**.
- `ttl > 0`: The item expires after the specified number of seconds.
- `ttl <= 0`: The item is considered **already expired**. Calling `set()` with this TTL acts as a no-op (it will not store the value, or it will immediately fail to retrieve it on the subsequent `get()`).

**Methods**
- `get(key: str) -> Optional[Any]`: Retrieves an item if it exists and has not expired.
- `set(key: str, value: Any, ttl: Optional[float] = None) -> None`: Stores an item respecting the TTL contract.
- `delete(key: str) -> bool`: Removes an item. Returns `True` if it existed, `False` otherwise.
- `clear() -> None`: Flushes all items from the cache.

### `stats()` Method (Snapshot Only)

Both the ABC and its implementations provide a `stats() -> dict` method. 
This method returns a **snapshot** of the cache's current state (e.g., `{"hits": 10, "misses": 2, "size": 5}`). It returns a new dictionary instance on every call; mutating the returned dict will not affect the cache's internal state.

### `MemoryBackend`

A concrete implementation of `CacheBackend` backed by a standard Python dictionary.
- Maintains internal dictionaries for values and their associated expiration timestamps.
- Evaluates TTL on read (`get`), automatically purging expired keys lazily.
- Accurately tracks hits, misses, and evictions for the `stats()` snapshot.

### Factory Function: `create_cache_backend`

To instantiate a cache, you must use the `create_cache_backend(backend_id: str)` factory function. 

**Security & Static Analysis**
The factory is strictly restricted to a static, literal-string allowlist of pre-imported classes. 
- **No dynamic imports:** The factory does *not* use `importlib` or `__import__`. All valid backend classes are imported at the top of the factory module.
- **No string evaluation:** Identifiers are matched via basic string equality (`==`) against a hardcoded tuple of allowed strings (e.g., `("memory",)`).

If a string is passed that is not in the static allowlist, the factory raises a `ValueError`.

## Usage Example

```python
from cache import create_cache_backend

# Initialize via the secure, static factory
cache = create_cache_backend("memory")

# Store items with different TTLs
cache.set("user:1", {"name": "Alice"}, ttl=3600)  # Expires in 1 hour
cache.set("config:app", {"debug": True}, ttl=None) # Never expires
cache.set("ephemeral", "temp", ttl=0)              # No-op: <= 0 is already expired

# Retrieve items
print(cache.get("user:1"))     # {'name': 'Alice'}
print(cache.get("ephemeral"))  # None

# Get a snapshot of cache statistics
print(cache.stats())
# Output example: {'hits': 1, 'misses': 1, 'size': 2}

# Invalid backend ID raises ValueError
# create_cache_backend("redis") -> ValueError: Unknown cache backend 'redis'. Allowed: ['memory']
```
