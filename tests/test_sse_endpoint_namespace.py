"""Tests for SSE `endpoint` event advertising (namespace binding).

Regression: sse_event_stream used to advertise a generic /message POST path on
every connection, so spec-compliant MCP clients (which POST wherever the
`endpoint` event points) lost the namespace and saw the unfiltered tool list.
A namespaced connection must advertise /sse/{ns}.
"""

from server.sse import sse_event_stream


class _FakeRequest:
    """Minimal stand-in for starlette Request for stream tests."""

    client = ("127.0.0.1", 12345)

    async def is_disconnected(self):
        return True


async def _first_event(namespace):
    """Consume only the first event yielded by sse_event_stream."""
    agen = sse_event_stream(_FakeRequest(), namespace, "[TEST]")
    event = await agen.__anext__()
    await agen.aclose()
    return event


async def test_namespaced_connection_advertises_namespaced_post_path():
    event = await _first_event("thinking")
    assert event == "event: endpoint\ndata: /sse/thinking\n\n"


async def test_bare_connection_still_advertises_message_path():
    event = await _first_event(None)
    assert event == "event: endpoint\ndata: /message\n\n"
