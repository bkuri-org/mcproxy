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


def test_single_payload_uses_full_budget_not_a_tenth():
    """Regression (imo incident 2026-08-24): a single-content MCP result ~11 KB
    was shape-previewed (<JSON: object ...>) under a 50 KB budget because the
    per-item cap was a flat 10% of remaining at every collection level. The cap
    must scale with collection size — a 1-element collection may use the full
    budget; the cumulative budget stays the sole governor."""
    big = {"count": 10, "results": [
        {"rid": f"r{i}", "title": "t" * 60, "text": "x" * 1000, "score": 0.1}
        for i in range(10)]}
    payload = {"content": [{"type": "text", "text": json.dumps(big)}], "isError": False}
    out = apply_result_limit(payload, 50_000)
    assert out["content"][0]["text"] == json.dumps(big)  # verbatim, no preview


def test_many_item_collection_still_spreads_budget():
    """The cap keeps its purpose for real collections: with N items, one item
    can't swallow the budget for the rest (N=10 → ~1/10 each, as before)."""
    items = [{"i": i, "blob": "word " * 1_800} for i in range(10)]  # 10 × 9 KB > 50 KB
    out = apply_result_limit({"rows": items}, 50_000)
    rows = out["rows"]
    assert len(rows) == 10                       # no row dropped outright
    assert any("…" in str(r) for r in rows)       # later rows clamp as budget runs out
    assert len(json.dumps(out)) < 52_000          # stays inside the budget


if __name__ == "__main__":
    test_small_json_passes_verbatim()
    test_oversized_json_gets_summarized()
    test_base64_always_placeholder()
    test_single_payload_uses_full_budget_not_a_tenth()
    test_many_item_collection_still_spreads_budget()
    print("OK")
