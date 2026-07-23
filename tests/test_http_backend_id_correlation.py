"""Regression: _send_request must correlate the response by JSON-RPC id.

mcp-yantrikdb (and other HTTP MCP servers) can emit notifications, or deliver a
stale/concurrent response ahead of the one that answers the current request.
Before the fix, _send_request returned the FIRST message carrying result/error,
ignoring id — an intermittent "id-mismatch desync" where a caller received a
different request's answer. These tests pin the id correlation.
"""

import json
from typing import Any, Dict, List

import pytest

from http_backend import HTTPServerConnector, _is_response_for


class _FakeResponse:
    def __init__(self, lines: List[bytes], content_type: str = "text/event-stream"):
        self._lines = lines
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.headers: Dict[str, str] = {}

    def post(self, url, json=None, headers=None, stream=False, timeout=None):
        return self._response


def _connector_with(lines: List[bytes]) -> HTTPServerConnector:
    conn = HTTPServerConnector(name="yantrikdb", url="http://x/mcp")
    conn.session = _FakeSession(_FakeResponse(lines))
    conn.session_id = "sess"
    return conn


def _sse(obj: Dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj).encode()


# --- pure helper ----------------------------------------------------------


class TestIsResponseFor:
    def test_matching_id_result(self):
        assert _is_response_for({"jsonrpc": "2.0", "id": "r1", "result": {}}, "r1")

    def test_matching_id_error(self):
        assert _is_response_for({"jsonrpc": "2.0", "id": "r1", "error": {}}, "r1")

    def test_mismatched_id_skipped(self):
        assert not _is_response_for(
            {"jsonrpc": "2.0", "id": "other", "result": {}}, "r1"
        )

    def test_notification_skipped(self):
        # notifications carry a method and must never be treated as a response
        assert not _is_response_for(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}, "r1"
        )

    def test_legacy_no_id_result_accepted(self):
        # non-conforming server that omits id on a result -> accept (don't break)
        assert _is_response_for({"jsonrpc": "2.0", "result": {}}, "r1")


# --- end-to-end id correlation -------------------------------------------


class TestSendRequestIdCorrelation:
    def test_returns_matching_result_not_first(self):
        """The desync repro: a stale/concurrent result arrives first, then a
        notification, then our matching response. We must return the third."""
        lines = [
            _sse({"jsonrpc": "2.0", "id": "call_recall_99", "result": "WRONG"}),
            _sse({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}),
            _sse({"jsonrpc": "2.0", "id": "call_recall_1", "result": "RIGHT"}),
        ]
        conn = _connector_with(lines)
        msg = conn._send_request(method="tools/call", id="call_recall_1")
        assert msg is not None
        assert msg["id"] == "call_recall_1"
        assert msg["result"] == "RIGHT"

    def test_unique_ids_per_call(self):
        """Concurrent same-tool calls must not share a JSON-RPC id."""
        conn = HTTPServerConnector(name="t", url="http://x/mcp")
        ids = [conn._next_id("call") for _ in range(3)]
        assert len(set(ids)) == 3
        assert all(i.startswith("call_") for i in ids)
