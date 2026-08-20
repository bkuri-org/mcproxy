import unittest
import threading
import time
from cache import CacheBackend, InMemoryCache


class TestCacheBackendABC(unittest.TestCase):
    """Tests for the CacheBackend Abstract Base Class."""

    def test_cannot_instantiate_abc(self):
        """CacheBackend is abstract and should not be instantiated directly."""
        with self.assertRaises(TypeError):
            CacheBackend()

    def test_can_instantiate_concrete_implementation(self):
        """A subclass implementing all abstract methods can be instantiated."""
        class DummyCache(CacheBackend):
            def get(self, key):
                return None
            def set(self, key, value, ttl=None):
                pass
            def delete(self, key):
                pass
            def clear(self):
                pass

        cache = DummyCache()
        self.assertIsInstance(cache, CacheBackend)


class TestInMemoryCache(unittest.TestCase):
    """Tests for the thread-safe InMemoryCache implementation."""

    def setUp(self):
        self.namespace = "test_ns"
        self.cache = InMemoryCache(namespace=self.namespace)

    def test_set_and_get(self):
        """Test setting a value and retrieving it."""
        self.cache.set("key1", "value1")
        self.assertEqual(self.cache.get("key1"), "value1")

    def test_get_missing_key(self):
        """Test retrieving a non-existent key returns None."""
        self.assertIsNone(self.cache.get("missing_key"))

    def test_set_overwrite(self):
        """Test that setting a key twice overwrites the previous value."""
        self.cache.set("key1", "value1")
        self.cache.set("key1", "value2")
        self.assertEqual(self.cache.get("key1"), "value2")

    def test_delete(self):
        """Test deleting an existing key."""
        self.cache.set("key1", "value1")
        self.cache.delete("key1")
        self.assertIsNone(self.cache.get("key1"))

    def test_delete_missing_key_does_not_raise(self):
        """Test that deleting a non-existent key fails silently."""
        self.cache.delete("non_existent_key")

    def test_clear(self):
        """Test clearing all keys in the cache."""
        self.cache.set("key1", "value1")
        self.cache.set("key2", "value2")
        self.cache.clear()
        self.assertIsNone(self.cache.get("key1"))
        self.assertIsNone(self.cache.get("key2"))

    def test_namespace_prefix(self):
        """Test that keys are stored with the namespace prefix."""
        self.cache.set("foo", "bar")
        expected_key = f"{self.namespace}:foo"
        self.assertIn(expected_key, self.cache._cache)
        self.assertEqual(self.cache._cache[expected_key], "bar")

    def test_no_namespace_prefix(self):
        """Test that keys are stored without a prefix if namespace is None."""
        cache_no_ns = InMemoryCache(namespace=None)
        cache_no_ns.set("foo", "bar")
        self.assertIn("foo", cache_no_ns._cache)
        self.assertNotIn(":", cache_no_ns._cache.keys())

    def test_namespace_isolation(self):
        """Test that two caches with different namespaces do not share state."""
        cache_a = InMemoryCache(namespace="app_a")
        cache_b = InMemoryCache(namespace="app_b")

        cache_a.set("shared_key", "value_a")
        cache_b.set("shared_key", "value_b")

        self.assertEqual(cache_a.get("shared_key"), "value_a")
        self.assertEqual(cache_b.get("shared_key"), "value_b")
        
        # Verify underlying storage is strictly isolated
        self.assertIn("app_a:shared_key", cache_a._cache)
        self.assertNotIn("app_b:shared_key", cache_a._cache)
        self.assertIn("app_b:shared_key", cache_b._cache)
        self.assertNotIn("app_a:shared_key", cache_b._cache)

    def test_thread_safety_set_and_get(self):
        """Test that concurrent sets and gets do not corrupt state or raise exceptions."""
        errors = []
        num_threads = 50
        iterations = 100

        def worker(thread_id):
            try:
                for i in range(iterations):
                    key = f"key_{thread_id}_{i}"
                    value = f"val_{thread_id}_{i}"
                    self.cache.set(key, value)
                    retrieved = self.cache.get(key)
                    self.assertEqual(retrieved, value)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(tid,))
            for tid in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "Errors occurred during concurrent cache operations")

    def test_thread_safety_concurrent_deletes(self):
        """Test that concurrent deletes on the same key do not raise exceptions."""
        errors = []
        num_threads = 50

        self.cache.set("concurrent_key", "value")

        def worker():
            try:
                for _ in range(50):
                    self.cache.delete("concurrent_key")
                    self.cache.set("concurrent_key", "value")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "Errors occurred during concurrent deletes")


if __name__ == '__main__':
    unittest.main()
