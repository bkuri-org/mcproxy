"""Tests for parallel.static_analyzer — AST-based static analysis.

Hermetic, module-scoped tests using in-memory sources and tmp_path.
No independence or concurrency logic is tested; only extraction,
graph building, side-effect detection, and serialization safety.
"""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Guards: ensure no sandbox/exec/eval leaks into the parallel package
# ---------------------------------------------------------------------------


def _walk_parallel_package_sources() -> list[tuple[str, str]]:
    """Yield (module_name, source_text) for every .py under parallel/."""
    pkg_root = Path(__file__).resolve().parent.parent / "parallel"
    sources: list[tuple[str, str]] = []
    for py_file in sorted(pkg_root.rglob("*.py")):
        rel = py_file.relative_to(pkg_root.parent)
        module = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        sources.append((module, py_file.read_text(encoding="utf-8")))
    return sources


class TestNoForbiddenImports:
    """Guard: parallel/ must never import sandbox modules or use exec/eval."""

    FORBIDDEN_IMPORTS = frozenset({
        "sandbox",
        "runpy",
        "code",
        "codeop",
        "subprocess",
        "multiprocessing",
    })

    FORBIDDEN_CALLS = frozenset({"exec", "eval", "compile", "__import__"})

    @pytest.fixture(scope="module")
    def parallel_sources(self):
        return _walk_parallel_package_sources()

    def test_no_forbidden_import_names(self, parallel_sources):
        for module, source in parallel_sources:
            tree = ast.parse(source, filename=module)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        base = alias.name.split(".")[0]
                        assert base not in self.FORBIDDEN_IMPORTS, (
                            f"{module}: forbidden import '{alias.name}'"
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        base = node.module.split(".")[0]
                        assert base not in self.FORBIDDEN_IMPORTS, (
                            f"{module}: forbidden from-import '{node.module}'"
                        )

    def test_no_exec_eval_calls(self, parallel_sources):
        for module, source in parallel_sources:
            tree = ast.parse(source, filename=module)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func_name = None
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_name = node.func.attr
                    if func_name in self.FORBIDDEN_CALLS:
                        assert False, (
                            f"{module}: forbidden call '{func_name}' "
                            f"at line {node.lineno}"
                        )


# ---------------------------------------------------------------------------
# Analyzer fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def analyzer():
    from parallel.static_analyzer import StaticAnalyzer

    return StaticAnalyzer()


@pytest.fixture(scope="module")
def sample_source():
    return textwrap.dedent("""\
        x = 1
        y = x + 2
        result = tool_call("do_thing", arg=y)
        z = result["value"]
        print(z)
        w = custom_tool("other")
    """)


# ---------------------------------------------------------------------------
# Tool-call extraction
# ---------------------------------------------------------------------------


class TestToolCallExtraction:
    """AST extraction of tool calls with anchored default + frozen allowlist."""

    def test_default_tool_call_detected(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        names = [tc.get("func_name") for tc in result.tool_calls]
        assert "tool_call" in names

    def test_allowlist_override_respected(self, sample_source):
        from parallel.static_analyzer import StaticAnalyzer

        allowlist = frozenset({"custom_tool"})
        sa = StaticAnalyzer(allowlist=allowlist)
        result = sa.analyze(sample_source, filename="<memory>")
        names = [tc.get("func_name") for tc in result.tool_calls]
        assert "custom_tool" in names
        assert "tool_call" not in names

    def test_allowlist_is_frozen(self):
        from parallel.static_analyzer import StaticAnalyzer

        sa = StaticAnalyzer()
        with pytest.raises(AttributeError):
            sa.allowlist.add("injected")  # type: ignore[attr-defined]

    def test_empty_source_yields_no_calls(self, analyzer):
        result = analyzer.analyze("", filename="<memory>")
        assert result.tool_calls == []

    def test_no_tool_calls_in_pure_arithmetic(self, analyzer):
        src = "a = 1\nb = a + 2\nc = b * 3\n"
        result = analyzer.analyze(src, filename="<memory>")
        assert result.tool_calls == []


# ---------------------------------------------------------------------------
# Variable-dependency graph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    """Variable-dependency graph built from AST assignments."""

    def test_graph_has_nodes_for_assignments(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        assert any("x" in n["id"] for n in result.graph_nodes)
        assert any("y" in n["id"] for n in result.graph_nodes)

    def test_graph_has_edges_for_dependencies(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        edges = result.graph_edges
        assert any(
            "x" in e["src"] and "y" in e["dst"] for e in edges
        )

    def test_empty_source_graph_empty(self, analyzer):
        result = analyzer.analyze("", filename="<memory>")
        assert result.graph_nodes == []
        assert result.graph_edges == []

    def test_isolated_var_has_no_edges(self, analyzer):
        src = "a = 1\nb = 2\n"
        result = analyzer.analyze(src, filename="<memory>")
        assert result.graph_edges == []
        assert len(result.graph_nodes) == 2


# ---------------------------------------------------------------------------
# Side-effect detection
# ---------------------------------------------------------------------------


class TestSideEffectDetection:
    """Detect obvious side effects: print, open-for-write, attribute set."""

    def test_print_detected(self, analyzer):
        src = 'print("hello")\n'
        result = analyzer.analyze(src, filename="<memory>")
        assert len(result.side_effects) == 1
        assert result.side_effects[0]["kind"] == "print"

    def test_open_write_detected(self, analyzer):
        src = 'f = open("/tmp/x", "w")\n'
        result = analyzer.analyze(src, filename="<memory>")
        kinds = {se["kind"] for se in result.side_effects}
        assert "open_write" in kinds

    def test_open_read_not_flagged(self, analyzer):
        src = 'f = open("/tmp/x", "r")\n'
        result = analyzer.analyze(src, filename="<memory>")
        kinds = {se["kind"] for se in result.side_effects}
        assert "open_write" not in kinds

    def test_attribute_assign_detected(self, analyzer):
        src = "obj.attr = 42\n"
        result = analyzer.analyze(src, filename="<memory>")
        kinds = {se["kind"] for se in result.side_effects}
        assert "attr_set" in kinds

    def test_pure_assignment_no_side_effects(self, analyzer):
        src = "a = 1\nb = a + 2\n"
        result = analyzer.analyze(src, filename="<memory>")
        assert result.side_effects == []


# ---------------------------------------------------------------------------
# Serializable dump — only ids and hashed locations
# ---------------------------------------------------------------------------


class TestSerializableDump:
    """Dump must contain only node/edge ids and hashed locations.

    No source text, no argument names, no literal values.
    """

    def test_dump_is_json_serializable(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        data = result.dump()
        json_str = json.dumps(data)
        assert isinstance(json_str, str)

    def test_dump_contains_no_source_text(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        data = result.dump()
        flat = json.dumps(data)
        # Literal argument strings must not leak into the dump
        assert '"do_thing"' not in flat
        assert '"other"' not in flat

    def test_dump_contains_no_arg_names(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        data = result.dump()
        flat = json.dumps(data)
        assert '"arg"' not in flat

    def test_dump_nodes_have_ids_and_hashed_locations(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        data = result.dump()
        for node in data.get("nodes", []):
            assert "id" in node
            assert "loc_hash" in node
            # loc_hash should look like a hex digest prefix
            assert len(node["loc_hash"]) >= 8
            assert all(c in "0123456789abcdef" for c in node["loc_hash"])

    def test_dump_edges_have_src_dst_ids(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        data = result.dump()
        for edge in data.get("edges", []):
            assert "src" in edge
            assert "dst" in edge

    def test_dump_tool_calls_have_hashed_locations(self, analyzer, sample_source):
        result = analyzer.analyze(sample_source, filename="<memory>")
        data = result.dump()
        for tc in data.get("tool_calls", []):
            assert "call_id" in tc
            assert "loc_hash" in tc
            assert "source" not in tc
            assert "args" not in tc


# ---------------------------------------------------------------------------
# Hermetic in-memory source tests with tmp_path
# ---------------------------------------------------------------------------


class TestHermeticInMemorySources:
    """Use tmp_path for any file I/O; analysis from in-memory strings."""

    def test_analyze_from_memory_string(self, analyzer):
        src = "val = tool_call('fn', x=1)\n"
        result = analyzer.analyze(src, filename="<memory>")
        assert len(result.tool_calls) == 1

    def test_analyze_from_tmp_file(self, analyzer, tmp_path):
        src = "a = 1\nb = tool_call('op')\n"
        p = tmp_path / "mod.py"
        p.write_text(src, encoding="utf-8")
        result = analyzer.analyze(src, filename=str(p))
        assert len(result.tool_calls) == 1

    def test_multiple_independent_analyses(self, analyzer):
        src_a = "x = tool_call('a')\n"
        src_b = "y = 1\n"
        r_a = analyzer.analyze(src_a, filename="<mem_a>")
        r_b = analyzer.analyze(src_b, filename="<mem_b>")
        assert len(r_a.tool_calls) == 1
        assert len(r_b.tool_calls) == 0

    def test_dump_round_trip_via_file(self, analyzer, tmp_path):
        src = "a = 1\nb = a + tool_call('f')\nprint(b)\n"
        result = analyzer.analyze(src, filename="<memory>")
        dump = result.dump()
        out = tmp_path / "dump.json"
        out.write_text(json.dumps(dump, indent=2), encoding="utf-8")
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded["nodes"] == dump["nodes"]
        assert loaded["edges"] == dump["edges"]


# ---------------------------------------------------------------------------
# AnalysisResult dataclass / named-tuple shape
# ---------------------------------------------------------------------------


class TestAnalysisResultShape:
    """Ensure the result object has the expected public attributes."""

    REQUIRED_ATTRS = (
        "tool_calls",
        "graph_nodes",
        "graph_edges",
        "side_effects",
        "dump",
    )

    def test_result_has_required_attributes(self, analyzer):
        result = analyzer.analyze("", filename="<memory>")
        for attr in self.REQUIRED_ATTRS:
            assert hasattr(result, attr), f"missing attribute: {attr}"

    def test_dump_returns_dict(self, analyzer):
        result = analyzer.analyze("", filename="<memory>")
        assert isinstance(result.dump(), dict)
