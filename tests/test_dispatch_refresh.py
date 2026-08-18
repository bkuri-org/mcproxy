"""dispatch action=refresh: re-fetch upstream tools and rebuild the manifest."""

import asyncio
from types import SimpleNamespace

import server
import server.lifecycle as lifecycle
from server.handlers.tools.refresh import handle_refresh


class FakeConnector(SimpleNamespace):
    pass


def _conn(name, tools, running=True, resp=None):
    c = FakeConnector(name=name, tools=list(tools), is_running=lambda: running)
    c._send_request = (
        lambda method, id, **kw: resp if resp is not None else {"result": {"tools": tools}}
    )
    return c


def _setup(conns, target_server=None):
    live = {n: c for n, c in conns.items() if c.is_running()}
    manager = SimpleNamespace(
        servers=conns,
        get_all_tools=lambda: {n: c.tools for n, c in live.items()},
    )
    lifecycle.server_manager = manager
    rebuilds = []
    server.refresh_manifest = lambda tools: rebuilds.append(tools)
    args = {"server": target_server} if target_server else {}
    return conns, rebuilds, args


def test_refresh_updates_tools_and_rebuilds_once():
    a = _conn("a", [{"name": "old"}], resp={"result": {"tools": [{"name": "new"}]}})
    b = _conn("b", [{"name": "x"}])
    conns, rebuilds, args = _setup({"a": a, "b": b})

    r = asyncio.run(handle_refresh(1, args))
    assert a.tools == [{"name": "new"}]  # stale list replaced
    assert len(rebuilds) == 1 and rebuilds[0] == {"a": [{"name": "new"}], "b": [{"name": "x"}]}
    summary = __import__("json").loads(r["result"]["content"][0]["text"])["refreshed"]
    assert summary == {"a": 1, "b": 1}


def test_down_server_skipped_and_reported():
    a = _conn("a", [{"name": "x"}])
    d = _conn("down", [], running=False)
    _, rebuilds, args = _setup({"a": a, "down": d})

    r = asyncio.run(handle_refresh(1, args))
    summary = __import__("json").loads(r["result"]["content"][0]["text"])["refreshed"]
    assert summary["down"].startswith("down")
    assert "down" not in rebuilds[0]


def test_single_server_target():
    a = _conn("a", [{"name": "old"}], resp={"result": {"tools": [{"name": "new"}]}})
    b = _conn("b", [{"name": "keep"}])
    _, rebuilds, _ = _setup({"a": a, "b": b}, target_server="b")

    asyncio.run(handle_refresh(1, {"server": "b"}))
    assert b.tools == [{"name": "keep"}]
    assert a.tools == [{"name": "old"}]  # a's connector untouched
    # manifest rebuilt from ALL running connectors, not just the target
    assert rebuilds[0] == {"a": [{"name": "old"}], "b": [{"name": "keep"}]} 


def test_unknown_server_rejected():
    a = _conn("a", [{"name": "x"}])
    _, rebuilds, _ = _setup({"a": a})

    r = asyncio.run(handle_refresh(1, {"server": "nope"}))
    assert "error" in r and r["error"]["code"] == -32602
    assert rebuilds == []
