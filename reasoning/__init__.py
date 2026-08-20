"""Built-in Reasoning Engine for mcproxy.

Provides a pluggable think/verify step before executing agent code.
Engine definitions are config-driven — users can add, remove, or
swap thinking servers without code changes.

Default engines:
  - sequential:  sequential_thinking.think (step-by-step reasoning)
  - simple:      think_tool.think (simple thinking)
  - decompose:   atom_of_thoughts.think (decompose into atomic thoughts)

Parameter generation:
  - param_gen: extract NL params, map to JSON Schema with exact-then-fuzzy
    matching, shallow validation, and missing-param prompt construction.
"""

import re
import time
from typing import Any, Callable, Dict, List, Optional

from logging_config import get_logger

from .intent import (  # noqa: E402
    Intent,
    IntentValidationError,
    build_classification_prompt,
    classify_intent,
    normalize_intent,
    validate_intent,
)
from .engine import (  # noqa: E402
    ThinkEngine,
    ThinkEngineRegistry,
)

logger = get_logger(__name__)

__all__ = [
    "ThinkEngine",
    "build_think_prompt",
    "Intent",
    "IntentValidationError",
    "build_classification_prompt",
    "classify_intent",
    "normalize_intent",
    "validate_intent",
]

# Default engine definitions — overridable in mcproxy.json reasoning.engines
DEFAULT_ENGINES: Dict[str, Dict[str, str]] = {
    "sequential": {
        "server": "sequential_thinking",
        "tool": "think",
        "description": "Step-by-step reasoning",
    },
    "simple": {
        "server": "think_tool",
        "tool": "think",
        "description": "Simple thinking",
    },
    "decompose": {
        "server": "atom_of_thoughts",
        "tool": "think",
        "description": "Decompose into atomic thoughts",
    },
}

# Default auto-think settings
DEFAULT_DANGEROUS_KEYWORDS: List[str] = [
    "delete",
    "drop",
    "remove",
    "production",
    "prod",
    "destroy",
    "purge",
    "wipe",
    "truncate",
]

DEFAULT_COMPLEXITY_THRESHOLD: int = 3

# ---------------------------------------------------------------------------
# Shared dry-run constants (re-exported from dry_run for fast access)
# ---------------------------------------------------------------------------
_DRY_RUN_ENABLED: bool = True  # toggled by config reasoning.dry_run.enabled


def _count_tool_calls(code: str) -> int:
    """Count the number of tool call expressions in code.

    Looks for patterns like api.server(), api.Server(), or bare .server().
    """
    return len(re.findall(r"\.server\(|api\.server\(|api\.Server\(", code))


def _has_dangerous_keywords(
    code: str, keywords: List[str]
) -> List[str]:
    """Check if code contains any dangerous keywords.

    Args:
        code: The code to analyze
        keywords: List of dangerous keywords to check

    Returns:
        List of matched dangerous keywords (empty if none)
    """
    code_lower = code.lower()
    matched = []
    for kw in keywords:
        # Simple substring match on word-like tokens
        kw_lower = kw.lower()
        if kw_lower in code_lower:
            matched.append(kw)
    return matched


def build_think_prompt(code: str, analysis: Optional[Dict] = None) -> str:
    """Build a thinking prompt from agent code and optional analysis.

    Args:
        code: The code the agent wants to execute
        analysis: Optional pre-analysis result (dangerous keywords, complexity)

    Returns:
        Formatted thinking prompt string
    """
    truncated = code[:4000] if len(code) > 4000 else code
    lines = [
        "Please analyze this code before execution:",
        "",
        "```python",
        truncated,
        "```",
    ]

    if analysis:
        lines.append("")
        lines.append("Preliminary analysis:")
        if analysis.get("dangerous_keywords"):
            lines.append(
                f"  - Dangerous keywords detected: "
                f"{', '.join(analysis['dangerous_keywords'])}"
            )
        if analysis.get("tool_calls", 0) > 0:
            lines.append(f"  - Tool calls: {analysis['tool_calls']}")
        if analysis.get("complexity_note"):
            lines.append(f"  - {analysis['complexity_note']}")

    if len(code) > 4000:
        lines.append("")
        lines.append(
            f"(Code truncated to 4000 chars from {len(code)} total)"
        )

    return "\n".join(lines)


class ThinkEngine:
    """Pluggable reasoning engine for pre-execution thinking.

    Loads engine definitions from config, calls the appropriate thinking
    server via tool_executor, and analyzes code for auto-think triggers.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        tool_executor: Callable,
    ):
        """Initialize the think engine.

        Args:
            config: mcproxy.json config dict (reasoning section optional)
            tool_executor: Callable(server_name, tool_name, args) -> result
        """
        reasoning_config = config.get("reasoning", {})

        # Load engines: user config overrides defaults
        user_engines = reasoning_config.get("engines", {})
        self.engines: Dict[str, Dict[str, str]] = dict(DEFAULT_ENGINES)
        self.engines.update(user_engines)

        self.default_engine = reasoning_config.get("default", "sequential")

        # Auto-think config
        auto_config = reasoning_config.get("auto_think", {})
        self.auto_enabled = auto_config.get("enabled", True)
        self.dangerous_keywords = auto_config.get(
            "keywords", DEFAULT_DANGEROUS_KEYWORDS
        )
        self.complexity_threshold = auto_config.get(
            "complexity_threshold", DEFAULT_COMPLEXITY_THRESHOLD
        )

        self.tool_executor = tool_executor

    def get_engine_names(self) -> List[str]:
        """Get list of configured engine names."""
        return list(self.engines.keys())

    def get_engine_info(self, name: str) -> Optional[Dict[str, str]]:
        """Get info about a specific engine."""
        return self.engines.get(name)

    def analyze_code(self, code: str) -> Dict[str, Any]:
        """Analyze code for auto-think triggers.

        Args:
            code: The code to analyze

        Returns:
            Analysis dict with:
                - should_think: bool — whether to auto-trigger
                - reason: str — why it triggered (or empty)
                - dangerous_keywords: list of matched keywords
                - tool_calls: count of tool calls
                - complexity_note: optional note about complexity
        """
        if not self.auto_enabled:
            return {
                "should_think": False,
                "reason": "auto-think disabled",
                "dangerous_keywords": [],
                "tool_calls": 0,
                "complexity_note": None,
            }

        dangerous = _has_dangerous_keywords(code, self.dangerous_keywords)
        tool_calls = _count_tool_calls(code)
        reasons: List[str] = []
        complexity_note: Optional[str] = None

        if dangerous:
            reasons.append(
                f"dangerous keywords: {', '.join(dangerous)}"
            )

        if tool_calls >= self.complexity_threshold:
            reasons.append(
                f"high complexity ({tool_calls} tool calls)"
            )
            complexity_note = f"{tool_calls} tool calls detected"

        should_think = bool(reasons)

        return {
            "should_think": should_think,
            "reason": "; ".join(reasons) if reasons else "",
            "dangerous_keywords": dangerous,
            "tool_calls": tool_calls,
            "complexity_note": complexity_note,
        }

    async def think(
        self,
        code: str,
        engine_name: Optional[str] = None,
        analysis: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Execute the thinking engine with the given code.

        Calls the configured thinking server's tool with a prompt
        derived from the agent code.

        Args:
            code: The code the agent wants to execute
            engine_name: Which engine to use (default: self.default_engine)
            analysis: Pre-computed analysis (optional, will compute if absent)

        Returns:
            Dict with:
                - engine: engine name used
                - server: server that was called
                - tool: tool that was called
                - result: the raw response from the thinking server
                - duration_ms: how long thinking took
                - prompt: what was sent to the thinking server
        """
        engine = engine_name or self.default_engine
        engine_config = self.engines.get(engine)
        if not engine_config:
            available = ", ".join(self.engines.keys())
            raise ValueError(
                f"Unknown thinking engine: '{engine}'. "
                f"Available engines: {available}"
            )

        if analysis is None:
            analysis = self.analyze_code(code)

        prompt = build_think_prompt(code, analysis)
        server_name = engine_config["server"]
        tool_name = engine_config["tool"]

        logger.info(
            f"[THINK] engine={engine} server={server_name} "
            f"code_len={len(code)} analysis={analysis.get('reason', '')}"
        )

        start = time.perf_counter()
        try:
            result = await self.tool_executor(
                server_name,
                tool_name,
                {"thought": prompt},
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.info(f"[THINK] completed in {duration_ms}ms")
            return {
                "engine": engine,
                "server": server_name,
                "tool": tool_name,
                "result": result,
                "duration_ms": duration_ms,
                "prompt": prompt,
            }
        except Exception as e:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.error(f"[THINK] engine {engine} failed after {duration_ms}ms: {e}")
            return {
                "engine": engine,
                "server": server_name,
                "tool": tool_name,
                "error": str(e),
                "duration_ms": duration_ms,
                "prompt": prompt,
            }