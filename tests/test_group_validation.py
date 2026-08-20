"""Group validation: isolated namespaces list plainly, '!' is a deprecated alias,
unknown refs are errors."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_watcher import validate_groups

NS = {
    "web": {"servers": [], "isolated": False},
    "trading": {"servers": [], "isolated": True},
}


def run(groups):
    return validate_groups(groups, NS, raise_on_error=False)


# isolated ns listed plainly: valid with warning (was an error before)
e, w = run({"ops": {"namespaces": ["web", "trading"]}})
assert not e, e
assert any("includes isolated namespace 'trading'" in x for x in w), w

# legacy '!' prefix: still valid, deprecation warning
e, w = run({"ops": {"namespaces": ["web", "!trading"]}})
assert not e, e
assert any("deprecated" in x for x in w), w

# unknown namespace: error
e, w = run({"ops": {"namespaces": ["web", "nope"]}})
assert any("unknown namespace" in x for x in e), e

print("test_group_validation: all passed")
