# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-03-03

### Breaking Changes

#### `mcproxy_sequence` transform variable renamed: `data` → `read_result`

**What changed:**
- In `mcproxy_sequence` transform code, the variable containing the extracted read result has been renamed from `data` to `read_result`

**Why:**
- `read_result` is self-documenting - agents immediately understand it's the result from the read step
- `data` was ambiguous - agents tried to access it like `data['content'][0]['text']` when it was already extracted
- Future-proof - works regardless of extraction format (text, json, binary, etc.)

**Migration:**
```python
# Before
mcproxy_sequence(
    read={...},
    transform='''
    config = json.loads(data)
    result = {"content": json.dumps(config)}
    ''',
    write={...}
)

# After
mcproxy_sequence(
    read={...},
    transform='''
    config = json.loads(read_result)
    result = {"content": json.dumps(config)}
    ''',
    write={...}
)
```

**Impact:**
- Any existing `mcproxy_sequence` transforms using `data` will break
- Simple find/replace: `data` → `read_result` in transform code
- Deployed 2026-03-03, minimal existing usage expected

### Added

- **Online blocklist**: Allowlisted HTTPS-only source with checksum-verified fetch and bundled seed; fail-closed (fatal on unreachable boot); enforced at both pre-dispatch and post-resolution
- **Container hardening**: Shared digest pins across all images, exact-version requirements (no floating tags), enumerated minimal capabilities, and dual-runtime docker + podman smoke gates
- **Project metadata**: Added `pyproject.toml` for proper version tracking and dependency management
- **uv support**: Recommended setup now uses `uv` for faster dependency installation
- **`mcproxy_sequence` single operations**: transform and write are now optional
- **Improved imports**: `json`, `re`, `sys` now available in execute sandbox without explicit imports
- **Better error messages**: Clear error when trying to access `tool_results` during execution

### Changed

- **Documentation restructured**: `sequence` is now the primary tool recommendation
- **Self-documenting variable names**: Reduces need for explanatory text

### Migration Guide

#### From v1.x to v2.0

1. **Update transform code**: Replace `data` with `read_result` in all `mcproxy_sequence` transforms
2. **Optional: Switch to uv**: Use `uv venv && uv pip install -e ".[dev]"` for faster setup
3. **Update imports**: Remove explicit `import json/re/sys` from execute code (now auto-available)

---
