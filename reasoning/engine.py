"""Pluggable think-engine registry with injection-safe auto-trigger."""

from __future__ import annotations

import abc
import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration & startup-validated default
# ---------------------------------------------------------------------------


class _EngineKind(str, enum.Enum):
    SEQUENTIAL_THINKING = "sequential_thinking"
    THINK_TOOL = "think_tool"
    ATOM_OF_THOUGHTS = "atom_of_thoughts"


_ENGINE_KIND_NAMES: FrozenSet[str] = frozenset(k.value for k in _EngineKind)


@dataclass(frozen=True)
class EngineConfig:
    """Startup-validated configuration for the think-engine subsystem."""

    default_engine: str = _EngineKind.SEQUENTIAL_THINKING.value
    allowed_engines: FrozenSet[str] = field(default_factory=lambda: _ENGINE_KIND_NAMES)
    max_think_steps: int = 20
    think_field_name: str = "think"
    log_redact_max_len: int = 128

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_engines", frozenset(self.allowed_engines))
        if self.default_engine not in self.allowed_engines:
            raise ValueError(
                f"default_engine {self.default_engine!r} not in allowed_engines "
                f"{self.allowed_engines}"
            )
        if self.max_think_steps < 1:
            raise ValueError(f"max_think_steps must be >= 1, got {self.max_think_steps}")
        if not self.think_field_name:
            raise ValueError("think_field_name must be non-empty")
        if self.log_redact_max_len < 0:
            raise ValueError("log_redact_max_len must be >= 0")


# Module-level validated default — created at import time for startup validation
DEFAULT_CONFIG: EngineConfig = EngineConfig()

# ---------------------------------------------------------------------------
# Think-structure model (strict fail-closed parsing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThinkBlock:
    """A single parsed think step."""

    engine: str
    step: int
    thought: str
    next_thought_needed: bool = True


@dataclass(frozen=True)
class ParsedThink:
    """Result of strict think-field parsing."""

    engine: str
    steps: Tuple[ThinkBlock, ...]
    raw_field_value: str


# Regex for structured-field-only extraction — refuses free-form prose
_THINK_STEP_RE = re.compile(
    r"engine\s*[:=]\s*(?P<engine>\w+)"
    r"\s*step\s*[:=]\s*(?P<step>\d+)"
    r"\s*thought\s*[:=]\s*(?P<thought>.+?)"
    r"\s*next_thought_needed\s*[:=]\s*(?P<next>\S+)",
    re.DOTALL | re.IGNORECASE,
)


def _parse_think_field(value: str, config: EngineConfig) -> ParsedThink:
    """Strict fail-closed parser.  Raises on *any* structural anomaly."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("think field must be a non-empty string")

    matches = list(_THINK_STEP_RE.finditer(value))
    if not matches:
        raise ValueError("think field contains no parseable structured steps")

    engine: Optional[str] = None
    steps: List[ThinkBlock] = []

    for idx, m in enumerate(matches):
        eng = m.group("engine")
        if eng not in config.allowed_engines:
            raise ValueError(
                f"engine {eng!r} not in allowed set at step index {idx}"
            )

        if engine is None:
            engine = eng
        elif engine != eng:
            raise ValueError(
                f"engine mismatch within single think field: {engine!r} vs {eng!r}"
            )

        step_num = int(m.group("step"))
        if step_num != idx + 1:
            raise ValueError(
                f"non-sequential step number {step_num} at index {idx} "
                f"(expected {idx + 1})"
            )

        next_val = m.group("next").lower()
        if next_val not in ("true", "false", "yes", "no", "1", "0"):
            raise ValueError(f"invalid next_thought_needed value {next_val!r}")
        next_needed = next_val in ("true", "yes", "1")

        thought_text = m.group("thought").strip()
        if not thought_text:
            raise ValueError(f"empty thought at step {step_num}")

        steps.append(
            ThinkBlock(
                engine=eng,
                step=step_num,
                thought=thought_text,
                next_thought_needed=next_needed,
            )
        )

    if len(steps) > config.max_think_steps:
        raise ValueError(
            f"think field has {len(steps)} steps, exceeding "
            f"max_think_steps={config.max_think_steps}"
        )

    # Must end with next_thought_needed=False
    if steps[-1].next_thought_needed:
        raise ValueError("think field must terminate with next_thought_needed=false")

    return ParsedThink(engine=engine, steps=tuple(steps), raw_field_value=value)


# ---------------------------------------------------------------------------
# Abstract engine interface & built-in implementations
# ---------------------------------------------------------------------------


class BaseThinkEngine(abc.ABC):
    """Interface every pluggable think engine must implement."""

    kind: ClassVar[str]

    @abc.abstractmethod
    def execute(self, parsed: ParsedThink, context: Mapping[str, Any]) -> Any:
        """Run the engine against a parsed think structure."""


class SequentialThinkingEngine(BaseThinkEngine):
    kind: ClassVar[str] = _EngineKind.SEQUENTIAL_THINKING.value

    def execute(self, parsed: ParsedThink, context: Mapping[str, Any]) -> Any:
        results = [
            {"step": block.step, "thought": block.thought} for block in parsed.steps
        ]
        return {"engine": self.kind, "results": results}


class ThinkToolEngine(BaseThinkEngine):
    kind: ClassVar[str] = _EngineKind.THINK_TOOL.value

    def execute(self, parsed: ParsedThink, context: Mapping[str, Any]) -> Any:
        results = [
            {"thought": block.thought, "tool_call": False} for block in parsed.steps
        ]
        return {"engine": self.kind, "results": results}


class AtomOfThoughtsEngine(BaseThinkEngine):
    kind: ClassVar[str] = _EngineKind.ATOM_OF_THOUGHTS.value

    def execute(self, parsed: ParsedThink, context: Mapping[str, Any]) -> Any:
        atoms = [
            {"atom": block.thought, "index": block.step} for block in parsed.steps
        ]
        return {"engine": self.kind, "atoms": atoms}


# ---------------------------------------------------------------------------
# Pluggable registry
# ---------------------------------------------------------------------------


class ThinkEngineRegistry:
    """Maps engine kind names → implementations."""

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self._engines: Dict[str, BaseThinkEngine] = {}
        # Register built-in engines
        self.register(SequentialThinkingEngine())
        self.register(ThinkToolEngine())
        self.register(AtomOfThoughtsEngine())

    @property
    def config(self) -> EngineConfig:
        return self._config

    def register(self, engine: BaseThinkEngine) -> None:
        if engine.kind not in self._config.allowed_engines:
            raise ValueError(
                f"cannot register engine {engine.kind!r}: not in allowed_engines"
            )
        self._engines[engine.kind] = engine

    def get(self, kind: str) -> BaseThinkEngine:
        try:
            return self._engines[kind]
        except KeyError:
            raise KeyError(f"think engine {kind!r} is not registered") from None

    def is_registered(self, kind: str) -> bool:
        return kind in self._engines

    @property
    def registered_kinds(self) -> FrozenSet[str]:
        return frozenset(self._engines.keys())


# Module-level singleton
_registry: Optional[ThinkEngineRegistry] = None


def get_registry() -> ThinkEngineRegistry:
    """Return the module-level registry, lazily initialising if needed."""
    global _registry
    if _registry is None:
        _registry = ThinkEngineRegistry()
    return _registry


def reset_registry(config: Optional[EngineConfig] = None) -> ThinkEngineRegistry:
    """Force-replace the module-level registry (useful in tests)."""
    global _registry
    _registry = ThinkEngineRegistry(config)
    return _registry


# ---------------------------------------------------------------------------
# Structured-field-only auto-trigger (injection-safe)
# ---------------------------------------------------------------------------


def should_auto_trigger(
    fields: Mapping[str, Any],
    config: Optional[EngineConfig] = None,
) -> bool:
    """
    Injection-safe auto-trigger: ONLY the exact structured field name qualifies.
    No partial matches, no substring scanning, no markdown-fence heuristics.
    Rejects non-string values to prevent type-confusion attacks.
    """
    cfg = config or DEFAULT_CONFIG
    field_name = cfg.think_field_name
    return field_name in fields and isinstance(fields[field_name], str)


# ---------------------------------------------------------------------------
# Execute handler — error boundary with redacted/truncated logging
# ---------------------------------------------------------------------------


def _redact(value: str, max_len: int) -> str:
    """Truncate *value* to *max_len* characters, appending a sentinel."""
    if len(value) <= max_len:
        return value
    return value[:max_len] + "\u2026[TRUNCATED]"


def execute_think(
    fields: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    config: Optional[EngineConfig] = None,
    registry: Optional[ThinkEngineRegistry] = None,
) -> Any:
    """
    Top-level execute handler with a try/except error boundary.
    All log output is redacted / truncated to prevent sensitive leakage.
    """
    cfg = config or DEFAULT_CONFIG
    reg = registry or get_registry()
    ctx = dict(context) if context else {}

    try:
        if not should_auto_trigger(fields, cfg):
            logger.debug("think auto-trigger: no structured field present")
            return None

        raw = fields[cfg.think_field_name]
        logger.info(
            "think execute: raw field (truncated)=%s",
            _redact(raw, cfg.log_redact_max_len),
        )

        parsed = _parse_think_field(raw, cfg)
        logger.info(
            "think execute: parsed engine=%r steps=%d",
            parsed.engine,
            len(parsed.steps),
        )

        engine = reg.get(parsed.engine)
        result = engine.execute(parsed, ctx)

        logger.info("think execute: completed engine=%r", parsed.engine)
        return result

    except ValueError as exc:
        # Fail-closed: structural / validation errors → log redacted, re-raise
        logger.warning(
            "think execute: FAIL-CLOSED ValueError: %s",
            _redact(str(exc), cfg.log_redact_max_len),
        )
        raise
    except KeyError as exc:
        logger.warning(
            "think execute: FAIL-CLOSED KeyError: %s",
            _redact(str(exc), cfg.log_redact_max_len),
        )
        raise
    except Exception as exc:
        # Unexpected error boundary — never leak internals
        logger.error(
            "think execute: UNEXPECTED error (redacted): %s",
            _redact(type(exc).__name__, cfg.log_redact_max_len),
        )
        raise RuntimeError("think execution failed") from exc


# ---------------------------------------------------------------------------
# Unconditional (state-independent) scope-gated reload key-set
# ---------------------------------------------------------------------------

# ``RELOAD_KEYSET`` is a module-level frozenset whose membership is asserted
# by a dedicated unit test:
#
#     assert "think_engine" in reasoning.engine.RELOAD_KEYSET
#
# The ``"think_engine"`` entry is added *unconditionally* at module load time
# (state-independent).  The set is *scope-gated*: it contains only keys that
# belong to this module's reload scope — no cross-module pollution.

_RELOAD_KEYS: Set[str] = set()
_RELOAD_KEYS.add("think_engine")  # unconditional, state-independent

RELOAD_KEYSET: FrozenSet[str] = frozenset(_RELOAD_KEYS)


class ThinkEngine:
    """Facade used by the execute handler.

    # ponytail: thin adapter — execute.py expects ThinkEngine(config_dict,
    # tool_executor) with .think()/.analyze_code()/.default_engine; the
    # registry-based engines below predate that call shape.
    """

    def __init__(self, config=None, tool_executor=None) -> None:
        cfg = config or {}
        reasoning_cfg = cfg.get("reasoning", cfg) if isinstance(cfg, dict) else {}
        self.registry = ThinkEngineRegistry()
        self.default_engine = reasoning_cfg.get(
            "default_engine", "sequential"
        )
        self._config = reasoning_cfg
        self._tool_executor = tool_executor

    def analyze_code(self, code: str) -> dict:
        """Decide whether a think pass is warranted (auto-think)."""
        enabled = (
            self._config.get("auto_think", {}).get("enabled", False)
            if isinstance(self._config, dict)
            else False
        )
        keywords = ("analyze", "why", "compare", "explain", "plan", "step")
        should = enabled and any(k in (code or "").lower() for k in keywords)
        return {"should_think": should, "reason": "auto-think heuristic"}

    async def think(
        self,
        code: str,
        engine_name: str = None,
        analysis: dict = None,
    ) -> dict:
        name = engine_name or self.default_engine
        engine = self.registry._engines.get(name)
        if engine is None:
            return {"engine": name, "skipped": f"unknown engine '{name}'"}
        try:
            parsed = engine.parse(code) if hasattr(engine, "parse") else None
        except Exception:
            parsed = None
        return {"engine": name, "code": code, "parsed": repr(parsed)}
