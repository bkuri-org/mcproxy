import abc
import threading
import time
import unittest


class CacheBackend(abc.ABC):
    """Abstract base class for cache backends."""

    @abc.abstractmethod
    def get(self, key: str):
        """Retrieve a value from the cache. Returns None if not found or expired."""
        pass

    @abc.abstractmethod
    def set(self, key: str, value, ttl: int = None) -> None:
        """Set a value in the cache with an optional Time-To-Live in seconds."""
        pass

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a value from the cache. Returns True if the key existed and was deleted."""
        pass

    @abc.abstractmethod
    def clear(self) -> None:
        """Clear all values in the cache namespace."""
        pass


class InMemoryCache(CacheBackend):
    """Thread-safe, in-memory cache implementation with namespace-prefixed keys."""

    def __init__(self, namespace: str = "default"):
        self._namespace = namespace
        self._store = {}
        self._lock = threading.Lock()

    def _prefixed_key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def get(self, key: str):
        prefixed_key = self._prefixed_key(key)
        with self._lock:
            item = self._store.get(prefixed_key)
            if item is None:
                return None
            
            value, expiry = item
            if expiry is not None and time.time() > expiry:
                del self._store[prefixed_key]
                return None
            
            return value

    def set(self, key: str, value, ttl: int = None) -> None:
        prefixed_key = self._prefixed_key(key)
        expiry = time.time() + ttl if ttl is not None else None
        
        with self._lock:
            self._store[prefixed_key] = (value, expiry)

    def delete(self, key: str) -> bool:
        prefixed_key = self._prefixed_key(key)
        with self._lock:
            return self._store.pop(prefixed_key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class TestInMemoryCache(unittest.TestCase):
    def setUp(self):
        self.cache = InMemoryCache(namespace="test")

    def test_set_and_get(self):
        self.assertIsNone(self.cache.get("key1"))
        self.cache.set("key1", "value1")
        self.assertEqual(self.cache.get("key1"), "value1")

    def test_namespace_prefixing(self):
        self.cache.set("mykey", "val")
        self.assertIn("test:mykey", self.cache._store)
        self.assertNotIn("mykey", self.cache._store)

    def test_delete(self):
        self.cache.set("key2", "value2")
        self.assertTrue(self.cache.delete("key2"))
        self.assertFalse(self.cache.delete("key2"))
        self.assertIsNone(self.cache.get("key2"))

    def test_clear(self):
        self.cache.set("k1", "v1")
        self.cache.set("k2", "v2")
        self.cache.clear()
        self.assertIsNone(self.cache.get("k1"))
        self.assertIsNone(self.cache.get("k2"))
        self.assertEqual(len(self.cache._store), 0)

    def test_ttl_expiration(self):
        self.cache.set("key3", "value3", ttl=1)
        self.assertEqual(self.cache.get("key3"), "value3")
        time.sleep(1.1)
        self.assertIsNone(self.cache.get("key3"))

    def test_no_ttl_expiration(self):
        self.cache.set("key4", "value4")
        time.sleep(0.1)
        self.assertEqual(self.cache.get("key4"), "value4")

    def test_thread_safety(self):
        def worker(cache, thread_id):
            for i in range(100):
                key = f"thread_{thread_id}_key_{i}"
                cache.set(key, i)
                self.assertEqual(cache.get(key), i)

        threads = []
        for t_id in range(10):
            t = threading.Thread(target=worker, args=(self.cache, t_id))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(self.cache._store), 1000)


if __name__ == "__main__":
    unittest.main()
