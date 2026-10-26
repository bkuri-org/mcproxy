#!/usr/bin/env bash
# Deterministic exposure gates against a RUNNING mcproxy (owner-blessed 261019).
# Usage: BASE=http://192.168.50.71:12010 NS=thinking ./scripts/verify_exposure.sh
# Gates: ordering stability, cache fields, describe→promote, dormant teach error.
set -euo pipefail
BASE="${BASE:-http://localhost:12010}"
NS="${NS:-thinking}"
SEAT="verify-$$"
DORMANT_SEAT="dormant-$$"

list() { # $1 seat → tools/list JSON
  curl -sf -m 30 -X POST "$BASE/message" \
    -H 'Content-Type: application/json' -H "X-Namespace: $NS" -H "X-Session-ID: $1" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
}

call() { # $1 seat, $2 payload → tools/call JSON
  curl -sf -m 60 -X POST "$BASE/message" \
    -H 'Content-Type: application/json' -H "X-Namespace: $NS" -H "X-Session-ID: $1" \
    -d "$2"
}

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

# --- 1. ordering stable across two calls ------------------------------------
L1=$(list "$SEAT"); L2=$(list "$SEAT")
[ "$L1" = "$L2" ] || fail "tools/list differs across two identical calls"
pass "ordering byte-identical across two calls"

# --- 2. exposure metadata + counts ------------------------------------------
L1=$(EXPOSURE_SEAT="$SEAT" python3 -c '
import json, os, sys
r = json.load(sys.stdin)["result"]
tools = r["tools"]
assert tools[0]["name"] == "dispatch", "gateway tool must stay first"
assert r.get("cacheScope") == "seat:" + os.environ["EXPOSURE_SEAT"], r.get("cacheScope")
assert isinstance(r.get("ttlMs"), int), "ttlMs missing"
dormant = r.get("dormant", [])
synth = next((t for t in tools if t["name"] == "dormant"), None)
assert (synth is not None) == bool(dormant), "shelf entry vs dormant[] mismatch"
if synth:
    for d in dormant:
        assert d["name"] in synth["description"], "shelf text missing " + d["name"]
size = len(json.dumps(r))
print(f"exposed={len(tools) - 1 - (1 if synth else 0)} dormant={len(dormant)} bytes={size} ~tokens={size//4}")
' <<<"$L1")
pass "cacheScope/ttlMs seat-scoped, shelf text matches dormant[]"
echo "  after: $L1"

# --- 3. describe promotes ----------------------------------------------------
call "$SEAT" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"dispatch","arguments":{"action":"inspect","server":"atom_of_thoughts"}}}' >/dev/null
AFTER=$(list "$SEAT")
echo "$AFTER" | python3 -c '
import json, sys
r = json.load(sys.stdin)["result"]
names = [t["name"] for t in r["tools"]]
aot = [n for n in names if n.startswith("atom_of_thoughts__")]
assert aot, "describe did not promote atom_of_thoughts tools; got: " + ", ".join(names)
print("describe promoted: " + ", ".join(aot))
'
pass "describe auto-promotes into the seat exposed set"

# --- 4. dormant teach error (fresh seat, so a dormant tool is still dormant) --
DORMANT_TOOL=$(echo "$AFTER" | python3 -c '
import json, sys
d = json.load(sys.stdin)["result"].get("dormant", [])
print(d[0]["name"] if d else "")
')
if [ -n "$DORMANT_TOOL" ]; then
  ERR=$(call "$DORMANT_SEAT" "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$DORMANT_TOOL\",\"arguments\":{}}}")
  echo "$ERR" | DORMANT_TOOL="$DORMANT_TOOL" python3 -c '
import json, os, sys
err = json.load(sys.stdin).get("error", {})
assert err.get("code") == -32000, err
assert "dormant" in err["message"] and "search/describe" in err["message"], err["message"]
tool = os.environ["DORMANT_TOOL"]
assert tool in err["message"] and err.get("data", {}).get("tool") == tool, err
'
  pass "direct dormant call → teach error naming the tool"

  ERR2=$(call "$DORMANT_SEAT" "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"$DORMANT_TOOL\",\"arguments\":{}}}")
  echo "$ERR2" | python3 -c '
import json, sys
assert "dormant — search/describe first" not in json.dumps(json.load(sys.stdin)), "second call still blocked"
'
  pass "second call after promotion is no longer blocked"
else
  echo "SKIP: no dormant tools in namespace $NS (shelf empty)"
fi

echo "ALL GATES GREEN (namespace=$NS base=$BASE)"
