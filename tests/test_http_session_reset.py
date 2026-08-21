"""Repro for the stale-session replay bug: initialize must never carry an
mcp-session-id header; a fresh one is captured from the response."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_backend


class _CaptureSession:
    """Stands in for requests.Session; records per-request headers."""

    def __init__(self, responder=None):
        self.headers = {}
        self._responder = responder
        self.seen = []

    def post(self, url, json=None, headers=None, **kw):
        self.seen.append({"method": json["method"], "headers": headers or {}})
        return self._responder(json)

    def close(self):
        pass


class _Resp:
    def __init__(self, session_id, payload):
        self.headers = {
            "mcp-session-id": session_id,
            "content-type": "application/json",
        }
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return {"jsonrpc": "2.0", "id": self._payload["id"], "result": {}}


def _responder(session_id):
    return lambda j: _Resp(session_id, j)


# initialize goes out headerless despite a stale id on the connector
c = http_backend.HTTPServerConnector(
    name="t", url="http://x/mcp", timeout=5, connect_timeout=1
)
c.session_id = "stale-id-from-dead-connection"
c.session = _CaptureSession(_responder("fresh-session-42"))
c._send_request(method="initialize", id="init")
assert c.session.seen and c.session.seen[-1]["headers"].get("mcp-session-id") is None, c.session.seen[-1]
assert c.session_id == "fresh-session-42"  # captured from the response

# the next request carries the fresh id
c._send_request(method="tools/list", id="2")
assert c.session.seen[-1]["headers"].get("mcp-session-id") == "fresh-session-42"

# start() drops a stale id before initializing (the actual bug fix)
c2 = http_backend.HTTPServerConnector(
    name="t2", url="http://x/mcp", timeout=5, connect_timeout=1
)
c2.session_id = "stale-id-2"
captured = _CaptureSession(_responder("brand-new"))
orig = http_backend.requests.Session
http_backend.requests.Session = lambda: captured
try:
    async def _noop_discover(*a, **k):
        pass

    c2._discover_tools = _noop_discover  # skip tool discovery I/O
    ok = asyncio.new_event_loop().run_until_complete(c2.start())
finally:
    http_backend.requests.Session = orig
assert ok, "start() failed with capture session"
init_req = [s for s in captured.seen if s["method"] == "initialize"][0]
assert init_req["headers"].get("mcp-session-id") is None, init_req
assert c2.session_id == "brand-new"

print("test_http_session_reset: all passed")
