"""Result limiter checks — the smallest cases that break if the governor drifts."""

import json

from server.result_limiter import apply_result_limit


def test_small_json_passes_verbatim():
    payload = json.dumps({"count": 1, "results": [{"rid": "x", "text": "hello world"}]})
    out = apply_result_limit({"content": [{"type": "text", "text": payload}]}, 50_000)
    assert out["content"][0]["text"] == payload  # must NOT become a <JSON: summary>


def test_oversized_json_gets_summarized():
    payload = json.dumps({"k": "v" * 100_000})
    out = apply_result_limit({"content": [{"type": "text", "text": payload}]}, 5_000)
    assert "<JSON:" in out["content"][0]["text"]
    assert len(out["content"][0]["text"]) < 5_000


def test_base64_always_placeholder():
    blob = "QUJDRA" * 100  # > _BASE64_MIN_LENGTH
    out = apply_result_limit({"t": blob}, 50_000)
    assert out["t"].startswith("<base64 data,")


if __name__ == "__main__":
    test_small_json_passes_verbatim()
    test_oversized_json_gets_summarized()
    test_base64_always_placeholder()
    print("OK")
