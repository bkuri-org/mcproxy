import time
import threading
import pytest

from session_vars import (
    SessionVariableStore,
    SessionExpiredError,
    SessionKeyError,
    SessionCapacityError,
    build_session_shim,
)


class TestSessionVariableStoreCreation:
    def test_store_initializes_empty(self):
        store = SessionVariableStore(max_sessions=100, max_keys_per_session=50)
        assert len(store) == 0

    def test_store_respects_configured_caps(self):
        store = SessionVariableStore(max_sessions=10, max_keys_per_session=20)
        assert store.max_sessions == 10
        assert store.max_keys_per_session == 20


class TestBindIdempotency:
    def test_bind_creates_session_dict(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="user-42")
        assert "sess-1" in store

    def test_bind_is_idempotent(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="user-42")
        store.bind("sess-1", principal="user-42")
        assert len(store) == 1
        # Existing data preserved across redundant bind
        store._sessions["sess-1"]["k"] = "v"
        store.bind("sess-1", principal="user-42")
        assert store._sessions["sess-1"]["k"] == "v"

    def test_bind_records_principal(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="user-42")
        assert store._sessions["sess-1"]._principal == "user-42"

    def test_bind_enforces_max_sessions_cap(self):
        store = SessionVariableStore(max_sessions=2)
        store.bind("s1", principal="u1")
        store.bind("s2", principal="u2")
        with pytest.raises(SessionCapacityError):
            store.bind("s3", principal="u3")


class TestSetRaisesOnAbsentSession:
    def test_set_raises_session_expired_when_no_session(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "nonexistent-session")
        with pytest.raises(SessionExpiredError):
            shim.set("key", "value")

    def test_set_raises_after_unbind(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        store.unbind("sess-1")
        shim = build_session_shim(store, "sess-1")
        with pytest.raises(SessionExpiredError):
            shim.set("key", "value")

    def test_set_works_after_bind(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("key", "value")
        assert shim.get("key") == "value"

    def test_set_never_recreates_orphaned_dict(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        store.unbind("sess-1")
        shim = build_session_shim(store, "sess-1")
        with pytest.raises(SessionExpiredError):
            shim.set("leak", "yes")
        # Confirm nothing leaked back into store
        assert "sess-1" not in store


class TestGetDeleteClearGracefulDegradation:
    def test_get_returns_default_when_session_absent(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "ghost")
        assert shim.get("key") is None
        assert shim.get("key", "fallback") == "fallback"

    def test_delete_silently_noops_when_session_absent(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "ghost")
        shim.delete("key")  # must not raise

    def test_clear_silently_noops_when_session_absent(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "ghost")
        shim.clear()  # must not raise

    def test_get_delete_clear_work_on_active_session(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("a", 1)
        shim.set("b", 2)
        assert shim.get("a") == 1
        shim.delete("a")
        assert shim.get("a") is None
        assert shim.get("b") == 2
        shim.clear()
        assert shim.get("b") is None


class TestKeyValidation:
    def test_set_rejects_invalid_key_type(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        with pytest.raises(SessionKeyError):
            shim.set(123, "bad")  # non-string key

    def test_set_rejects_empty_key(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        with pytest.raises(SessionKeyError):
            shim.set("", "bad")

    def test_set_rejects_key_with_null_byte(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        with pytest.raises(SessionKeyError):
            shim.set("k\0y", "bad")

    def test_set_rejects_overly_long_key(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        with pytest.raises(SessionKeyError):
            shim.set("x" * 257, "bad")

    def test_get_delete_accept_valid_keys_gracefully(self):
        store = SessionVariableStore()
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("valid_key-1", "ok")
        assert shim.get("valid_key-1") == "ok"
        shim.delete("valid_key-1")
        assert shim.get("valid_key-1") is None


class TestKeyCountCaps:
    def test_set_enforces_max_keys_per_session(self):
        store = SessionVariableStore(max_keys_per_session=2)
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("a", 1)
        shim.set("b", 2)
        with pytest.raises(SessionCapacityError):
            shim.set("c", 3)

    def test_delete_frees_slot_for_new_key(self):
        store = SessionVariableStore(max_keys_per_session=2)
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("a", 1)
        shim.set("b", 2)
        shim.delete("a")
        shim.set("c", 3)  # should succeed
        assert shim.get("c") == 3

    def test_clear_frees_all_slots(self):
        store = SessionVariableStore(max_keys_per_session=2)
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("a", 1)
        shim.set("b", 2)
        shim.clear()
        shim.set("c", 3)
        shim.set("d", 4)
        assert shim.get("c") == 3
        assert shim.get("d") == 4

    def test_set_overwrite_existing_key_does_not_increase_count(self):
        store = SessionVariableStore(max_keys_per_session=2)
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("a", 1)
        shim.set("b", 2)
        shim.set("a", "updated")  # overwrite, not new
        assert shim.get("a") == "updated"


class TestRaceSafeExpiry:
    def test_expired_session_removed_on_access(self):
        store = SessionVariableStore(default_ttl_seconds=0.05)
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        shim.set("k", "v")
        time.sleep(0.1)
        # get should degrade gracefully even after expiry
        assert shim.get("k") is None

    def test_set_on_expired_session_raises_session_expired(self):
        store = SessionVariableStore(default_ttl_seconds=0.05)
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        time.sleep(0.1)
        with pytest.raises(SessionExpiredError):
            shim.set("k", "v")

    def test_concurrent_binds_dont_duplicate_sessions(self):
        store = SessionVariableStore()
        errors = []

        def bind_loop(session_id, principal, count):
            for _ in range(count):
                try:
                    store.bind(session_id, principal=principal)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=bind_loop, args=("sess-1", "u1", 200))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        assert len(store) == 1

    def test_concurrent_sets_and_expires_are_safe(self):
        store = SessionVariableStore(
            default_ttl_seconds=0.1, max_keys_per_session=100
        )
        store.bind("sess-1", principal="u1")
        shim = build_session_shim(store, "sess-1")
        errors = []

        def writer(i):
            try:
                shim.set(f"key-{i}", i)
            except SessionExpiredError:
                pass  # expected after expiry
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        time.sleep(0.15)  # let sessions expire
        for t in threads:
            t.join()
        assert len(errors) == 0


class TestSessionStashLifecycle:
    def test_expire_all_clears_store(self):
        store = SessionVariableStore()
        store.bind("s1", principal="u1")
        store.bind("s2", principal="u2")
        store.expire_all()
        assert len(store) == 0

    def test_expire_session_removes_single_session(self):
        store = SessionVariableStore()
        store.bind("s1", principal="u1")
        store.bind("s2", principal="u2")
        store.expire_session("s1")
        assert "s1" not in store
        assert "s2" in store

    def test_shim_sees_expiry_from_stash(self):
        store = SessionVariableStore()
        store.bind("s1", principal="u1")
        shim = build_session_shim(store, "s1")
        shim.set("k", "v")
        store.expire_session("s1")
        with pytest.raises(SessionExpiredError):
            shim.set("k2", "v2")
        assert shim.get("k") is None

    def test_rebind_after_expiry_works(self):
        store = SessionVariableStore()
        store.bind("s1", principal="u1")
        store.expire_session("s1")
        store.bind("s1", principal="u1")
        shim = build_session_shim(store, "s1")
        shim.set("fresh", "data")
        assert shim.get("fresh") == "data"


class TestShimIsConstrained:
    def test_shim_has_exactly_four_methods(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "irrelevant")
        assert set(dir(shim)) & {"get", "set", "delete", "clear"}
        # No dict-like mutation access
        assert not hasattr(shim, "__setitem__")
        assert not hasattr(shim, "__getitem__")
        assert not hasattr(shim, "update")
        assert not hasattr(shim, "pop")

    def test_shim_cannot_access_other_sessions(self):
        store = SessionVariableStore()
        store.bind("s1", principal="u1")
        store.bind("s2", principal="u2")
        shim1 = build_session_shim(store, "s1")
        shim2 = build_session_shim(store, "s2")
        shim1.set("secret", "from-s1")
        assert shim2.get("secret") is None

    def test_shim_repr_does_not_leak_data(self):
        store = SessionVariableStore()
        store.bind("s1", principal="u1")
        shim = build_session_shim(store, "s1")
        shim.set("pw", "hunter2")
        r = repr(shim)
        assert "hunter2" not in r
        assert "pw" not in r


class TestSandboxExecGlobalsIntegration:
    def test_shim_insertable_into_exec_globals(self):
        store = SessionVariableStore()
        store.bind("sess-x", principal="u1")
        shim = build_session_shim(store, "sess-x")
        globs = {"session": shim, "result": None}
        exec("session.set('x', 42); result = session.get('x')", globs)
        assert globs["result"] == 42

    def test_sandbox_cannot_bypass_shim_to_reach_store(self):
        store = SessionVariableStore()
        store.bind("sess-x", principal="u1")
        shim = build_session_shim(store, "sess-x")
        globs = {"session": shim, "leaked": None}
        exec(
            "try:\n"
            "    leaked = session._store\n"
            "except AttributeError:\n"
            "    leaked = 'safe'\n",
            globs,
        )
        assert globs["leaked"] == "safe"

    def test_sandbox_set_after_expired_raises_in_exec(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "never-bound")
        globs = {"session": shim, "err": None}
        exec(
            "try:\n"
            "    session.set('x', 1)\n"
            "except Exception as e:\n"
            "    err = type(e).__name__\n",
            globs,
        )
        assert globs["err"] == "SessionExpiredError"

    def test_sandbox_graceful_get_on_missing_session(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "ghost")
        globs = {"session": shim, "val": "sentinel"}
        exec("val = session.get('any')", globs)
        assert globs["val"] is None

    def test_sandbox_graceful_delete_and_clear_on_missing_session(self):
        store = SessionVariableStore()
        shim = build_session_shim(store, "ghost")
        globs = {"session": shim, "ok": False}
        exec("session.delete('any'); session.clear(); ok = True", globs)
        assert globs["ok"] is True

    def test_principal_bound_at_bind_time_not_overridable_via_shim(self):
        store = SessionVariableStore()
        store.bind("sess-x", principal="u1")
        shim = build_session_shim(store, "sess-x")
        globs = {"session": shim, "p": None}
        exec(
            "try:\n"
            "    session._principal = 'attacker'\n"
            "except AttributeError:\n"
            "    p = 'safe'\n",
            globs,
        )
        assert globs["p"] == "safe"
        assert store._sessions["sess-x"]._principal == "u1"
