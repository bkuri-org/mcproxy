"""Health-check tool refresh: tools/list responses update the cached tool list."""

import asyncio

from http_backend import HTTPServerConnector


def _connector(tools):
    c = HTTPServerConnector(name="test", url="http://localhost:9")
    c._initialized = True
    from types import SimpleNamespace
    c.session = SimpleNamespace(close=lambda: None)  # pass is_running()
    c._send_request = lambda method, id, **kw: {"result": {"tools": tools}}
    return c


def test_tools_change_fires_callback_and_updates_cache():
    fired = []
    c = _connector([{"name": "a"}])
    c._on_tools_changed = lambda name, n: fired.append((name, n))

    asyncio.run(c._perform_health_check())
    assert [t["name"] for t in c.tools] == ["a"]
    assert fired == [("test", 1)]

    # Same tool set again: no duplicate callback
    asyncio.run(c._perform_health_check())
    assert len(fired) == 1

    # Tool set changes: cache updates, callback refires
    c._send_request = lambda method, id, **kw: {
        "result": {"tools": [{"name": "a"}, {"name": "b"}]}
    }
    asyncio.run(c._perform_health_check())
    assert [t["name"] for t in c.tools] == ["a", "b"]
    assert fired[-1] == ("test", 2)


def test_empty_tools_list_keeps_last_known():
    """Empty tools/list = upstream mid-restart; must not wipe the catalog."""
    c = _connector([{"name": "a"}])
    asyncio.run(c._perform_health_check())

    c._send_request = lambda method, id, **kw: {"result": {"tools": []}}
    asyncio.run(c._perform_health_check())
    assert [t["name"] for t in c.tools] == ["a"]


def test_health_failure_marks_disconnected():
    c = _connector([{"name": "a"}])
    c._send_request = lambda method, id, **kw: {"error": {"code": -1}}
    asyncio.run(c._perform_health_check())
    assert not c.is_running()
