"""Tests for reasoning/__init__.py — pluggable think engine."""

import pytest
from reasoning import (
    ThinkEngine,
    build_think_prompt,
    _has_dangerous_keywords,
    _count_tool_calls,
    DEFAULT_ENGINES,
    DEFAULT_DANGEROUS_KEYWORDS,
)


class TestDangerousKeywords:
    """Detection of dangerous keywords in code."""

    def test_dangerous_found(self):
        kw = _has_dangerous_keywords(
            'api.server("db").delete_record(id=5)',
            ["delete", "drop", "remove"],
        )
        assert "delete" in kw

    def test_no_match(self):
        kw = _has_dangerous_keywords(
            'print("hello world")',
            ["delete", "drop", "remove"],
        )
        assert kw == []

    def test_substring_not_false_positive(self):
        kw = _has_dangerous_keywords(
            'x = "deleted"',  # 'deleted' contains 'delete' as substring
            ["deleted_file"],
        )
        assert kw == []

    def test_multiple_keywords(self):
        kw = _has_dangerous_keywords(
            'api.server("db").delete(id=1); api.server("fs").remove(path="/tmp")',
            ["delete", "remove", "drop"],
        )
        assert "delete" in kw
        assert "remove" in kw
        assert "drop" not in kw


class TestToolCallCounting:
    """Counting tool call expressions in code."""

    def test_single_call(self):
        assert _count_tool_calls('api.server("wikipedia").search(query="x")') == 1

    def test_multiple_calls(self):
        code = """
        a = api.server("s1").tool1()
        b = api.server("s2").tool2()
        c = api.server("s3").tool3()
        """
        assert _count_tool_calls(code) == 3

    def test_no_calls(self):
        assert _count_tool_calls("x = 1 + 2") == 0


class TestBuildPrompt:
    """Thinking prompt construction."""

    def test_basic_prompt(self):
        prompt = build_think_prompt('print("hello")', None)
        assert "analyze this code" in prompt.lower()
        assert 'print("hello")' in prompt

    def test_with_analysis(self):
        prompt = build_think_prompt(
            'api.server("db").delete(id=5)',
            {
                "dangerous_keywords": ["delete"],
                "tool_calls": 1,
                "complexity_note": None,
            },
        )
        assert "delete" in prompt
        assert "Dangerous keywords" in prompt

    def test_truncated_long_code(self):
        long_code = "# " + "x" * 5000
        prompt = build_think_prompt(long_code, None)
        assert len(prompt) < 4500
        assert "truncated" in prompt

    def test_high_complexity_note(self):
        prompt = build_think_prompt(
            'api.server("a").t(); api.server("b").t(); api.server("c").t()',
            {
                "dangerous_keywords": [],
                "tool_calls": 3,
                "complexity_note": "3 tool calls detected",
            },
        )
        assert "3 tool calls" in prompt


class TestThinkEngine:
    """ThinkEngine configuration and code analysis."""

    def test_default_engines(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        names = engine.get_engine_names()
        assert "sequential" in names
        assert "simple" in names
        assert "decompose" in names
        assert engine.default_engine == "sequential"

    def test_user_engine_overrides_default(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        config = {
            "reasoning": {
                "default": "my_custom",
                "engines": {
                    "my_custom": {"server": "my_think", "tool": "reason"},
                },
            },
        }
        engine = ThinkEngine(config, mock_executor)
        assert "my_custom" in engine.get_engine_names()
        assert engine.default_engine == "my_custom"
        # Defaults still present unless overridden by same name
        assert "sequential" in engine.get_engine_names()

    def test_user_engine_overrides_same_name(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        config = {
            "reasoning": {
                "engines": {
                    "sequential": {"server": "my_think", "tool": "my_tool"},
                },
            },
        }
        engine = ThinkEngine(config, mock_executor)
        info = engine.get_engine_info("sequential")
        assert info["server"] == "my_think"  # User override wins
        assert info["tool"] == "my_tool"

    def test_auto_think_default_config(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        assert engine.auto_enabled is True
        assert engine.complexity_threshold == 3
        assert "delete" in engine.dangerous_keywords

    def test_auto_think_custom_config(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        config = {
            "reasoning": {
                "auto_think": {
                    "enabled": True,
                    "keywords": ["custom_kw"],
                    "complexity_threshold": 5,
                },
            },
        }
        engine = ThinkEngine(config, mock_executor)
        assert engine.dangerous_keywords == ["custom_kw"]
        assert engine.complexity_threshold == 5

    def test_analyze_dangerous_code(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        analysis = engine.analyze_code('api.server("db").delete_record(id=5)')
        assert analysis["should_think"] is True

    def test_analyze_safe_code(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        analysis = engine.analyze_code('x = 1 + 2')
        assert analysis["should_think"] is False

    def test_analyze_high_complexity(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        code = """
        a = api.server("s1").t()
        b = api.server("s2").t()
        c = api.server("s3").t()
        d = api.server("s4").t()
        """
        analysis = engine.analyze_code(code)
        assert analysis["should_think"] is True
        assert "tool calls" in analysis["reason"]

    def test_auto_think_disabled(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        config = {"reasoning": {"auto_think": {"enabled": False}}}
        engine = ThinkEngine(config, mock_executor)
        analysis = engine.analyze_code('api.server("db").delete_record(id=5)')
        assert analysis["should_think"] is False

    @pytest.mark.asyncio
    async def test_think_execution(self):
        async def mock_executor(server, tool, args):
            return {"thought": args["thought"], "status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        result = await engine.think('api.server("x").test()')
        assert result["engine"] == "sequential"
        assert result["server"] == "sequential_thinking"
        assert result["result"]["status"] == "ok"
        assert result["duration_ms"] >= 0
        assert "prompt" in result

    @pytest.mark.asyncio
    async def test_think_with_specific_engine(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        result = await engine.think('print("hello")', engine_name="simple")
        assert result["engine"] == "simple"
        assert result["server"] == "think_tool"

    @pytest.mark.asyncio
    async def test_unknown_engine(self):
        async def mock_executor(server, tool, args):
            return {"status": "ok"}

        engine = ThinkEngine({}, mock_executor)
        with pytest.raises(ValueError, match="Unknown thinking engine"):
            await engine.think("test", engine_name="nonexistent")

    @pytest.mark.asyncio
    async def test_think_engine_failure(self):
        async def mock_executor(server, tool, args):
            raise RuntimeError("Server unreachable")

        engine = ThinkEngine({}, mock_executor)
        result = await engine.think("test")
        assert "error" in result
        assert "Server unreachable" in result["error"]
        assert result["duration_ms"] >= 0