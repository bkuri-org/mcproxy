"""
Compositions engine — load, execute, substitute, nest.

Exposes a single generic ``composition`` tool via tool_aggregator.

Substitution syntax (opt-in, only inside ``${…}``):
    ${.arg}          → top-level argument ``arg``
    ${.arg.path}     → nested path into argument
    ${[N].path}      → index N of an array-valued context entry, then path
    ${step_key[N]}   → bracket indexing inside any path segment
    $${              → literal ``${`` (escaped)

Result normalisation (decided up front, per step and final result):
    1. If value is a dict with a ``text`` key → extract text.
    2. If value is a dict with a ``content`` list (OpenAI-style blocks)
       → concatenate ``type: "text"`` blocks.
    3. Attempt ``json.loads`` on the extracted string.
    4. Fall back to the raw value.

Debug output is confined to the response envelope — never printed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple, Union

# ---------------------------------------------------------------------------
# Path resolution (dot + bracket notation)
# ---------------------------------------------------------------------------

_PATH_SEG_RE = re.compile(r"[^.\[\]]+|\[\d+\]")


def _resolve_path(obj: Any, path: str) -> Any:
    """Walk *path* into *obj*, supporting ``.key`` and ``[N]`` notation."""
    for seg in _PATH_SEG_RE.findall(path):
        if seg.startswith("[") and seg.endswith("]"):
            idx = int(seg[1:-1])
            if not isinstance(obj, (list, tuple)):
                raise KeyError(f"Cannot index with {seg} on {type(obj).__name__}")
            obj = obj[idx]
        else:
            if isinstance(obj, dict):
                obj = obj[seg]
            elif isinstance(obj, (list, tuple)):
                obj = obj[int(seg)]
            else:
                raise KeyError(f"Cannot resolve '{seg}' on {type(obj).__name__}")
    return obj

# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------

_SUBST_RE = re.compile(r"\$\$(?=\{)|\$\{([^}]*)\}")


def substitute(template: str, context: Any) -> str:
    """
    Opt-in ``${…}`` substitution inside *template*.

    *context* is typically a ``dict`` (args + step outputs) but may be a
    ``list`` when the composition is called with array args.

    Unresolvable expressions are left as-is.
    """
    def _replacer(match: re.Match) -> str:
        # Escaped dollar-curly → literal ${
        if match.group(0) == "$${":
            return "${"

        expr = match.group(1)
        if not expr:
            return "${}"

        # --- ${.arg} or ${.arg.deep.path} ---
        if expr.startswith("."):
            path = expr[1:]
            try:
                val = _resolve_path(context, path)
            except (KeyError, IndexError, ValueError, TypeError):
                return match.group(0)
            return val if isinstance(val, str) else str(val)

        # --- ${[N].path}  (array context or array-valued step output) ---
        if expr.startswith("["):
            try:
                val = _resolve_path(context, expr)
            except (KeyError, IndexError, ValueError, TypeError):
                return match.group(0)
            return val if isinstance(val, str) else str(val)

        # --- ${key} or ${key.path} or ${key[0].path} ---
        try:
            val = _resolve_path(context, expr)
        except (KeyError, IndexError, ValueError, TypeError):
            return match.group(0)
        return val if isinstance(val, str) else str(val)

    return _SUBST_RE.sub(_replacer, template)


def _deep_substitute(obj: Any, context: Any) -> Any:
    """Recursively apply ``substitute`` to every string in *obj*."""
    if isinstance(obj, str):
        return substitute(obj, context)
    if isinstance(obj, dict):
        return {k: _deep_substitute(v, context) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_substitute(v, context) for v in obj]
    return obj

# ---------------------------------------------------------------------------
# Result normalisation
# ---------------------------------------------------------------------------


def normalize_result(raw: Any) -> Any:
    """
    Content-block aware normalisation applied **up front** to every
    intermediate and final result.

    Order:
        1. Extract text from known content-block shapes.
        2. Attempt JSON parse on the extracted string.
        3. Raw fallback.
    """
    # --- content-block extraction ---
    if isinstance(raw, dict):
        # {"text": "..."} shape
        if "text" in raw:
            raw = raw["text"]
        # OpenAI-style {"content": [{"type": "text", "text": "..."}, ...]}
        elif "content" in raw and isinstance(raw["content"], list):
            texts: List[str] = []
            for block in raw["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            if texts:
                raw = "\n".join(texts)

    # --- JSON parse attempt ---
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped and (stripped[0] in ("{", "[")):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
        return raw

    return raw

# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

CompositionDef = Dict[str, Any]


def load_composition(source: Union[str, Dict[str, Any]]) -> CompositionDef:
    """
    Load a composition from a JSON string or plain dict.

    Expected shape::

        {
            "steps": [
                {"tool": "<name>", "args": {...}, "output_key": "<key>"},
                ...
            ],
            "result": "<expr>" | {...}          (optional, defaults to last step)
        }
    """
    if isinstance(source, str):
        source = json.loads(source)
    if not isinstance(source, dict):
        raise TypeError("Composition source must be a dict or JSON string")
    if "steps" not in source:
        raise ValueError("Composition must contain a 'steps' key")
    steps = source["steps"]
    if not isinstance(steps, list):
        raise ValueError("'steps' must be a list")
    return source

# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


async def execute_composition(
    comp: CompositionDef,
    args: Union[Dict[str, Any], List[Any]],
    tool_execute_fn: Any,  # async (tool_name, tool_args) -> Any
    *,
    depth: int = 0,
    max_depth: int = 10,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """
    Run every step in *comp*, substituting with *args* + accumulated
    outputs.  Returns ``(normalised_result, debug_log)``.

    Nesting: if a step's ``tool`` is ``"composition"``, the step is
    executed recursively via this same function (depth-gated).
    """
    if depth > max_depth:
        raise RecursionError(
            f"Composition nesting exceeded max depth ({max_depth})"
        )

    # Build substitution context: start with args, then overlay step outputs.
    if isinstance(args, list):
        context: Any = list(args)
    else:
        context = dict(args)

    steps: List[Dict[str, Any]] = comp["steps"]
    debug: List[Dict[str, Any]] = []

    for i, step in enumerate(steps):
        tool_name: str = step.get("tool", "")
        raw_args: Any = step.get("args", {})
        output_key: str = step.get("output_key", f"_step_{i}")

        # --- substitute into args ---
        if isinstance(raw_args, str):
            substituted_args: Any = substitute(raw_args, context)
        elif isinstance(raw_args, (dict, list)):
            substituted_args = _deep_substitute(raw_args, context)
        else:
            substituted_args = raw_args

        # --- nested composition ---
        if tool_name == "composition":
            if isinstance(substituted_args, dict):
                nested_source = substituted_args.get(
                    "composition",
                    substituted_args.get(
                        "source",
                        substituted_args.get("definition", {}),
                    ),
                )
                nested_args = substituted_args.get("args", {})
            else:
                # args might be the composition directly
                nested_source = substituted_args
                nested_args = {}

            nested_comp = load_composition(nested_source)
            result, nested_debug = await execute_composition(
                nested_comp,
                nested_args,
                tool_execute_fn,
                depth=depth + 1,
                max_depth=max_depth,
            )
            debug.extend(
                {**d, "_nested_under": output_key} for d in nested_debug
            )
        else:
            # --- regular tool call ---
            if not isinstance(substituted_args, dict):
                substituted_args = {"_raw": substituted_args}
            result = await tool_execute_fn(tool_name, substituted_args)

        # --- normalise & store ---
        result = normalize_result(result)

        # Store into context (dict or list)
        if isinstance(context, dict):
            context[output_key] = result
        # For list contexts we don't append — step outputs are always
        # keyed, so we switch to dict mode after first step.
        else:
            context = dict(context)  # copy list items as {0: ..., 1: ...}
            context[output_key] = result

        debug.append({
            "step": i,
            "tool": tool_name,
            "output_key": output_key,
            "result_type": type(result).__name__,
        })

    # --- resolve final result ---
    result_expr = comp.get("result")
    if result_expr is None:
        # Default: last step's output
        last_key = f"_step_{len(steps) - 1}"
        final = context.get(last_key) if isinstance(context, dict) else None
    elif isinstance(result_expr, str):
        final = substitute(result_expr, context)
        final = normalize_result(final)
    elif isinstance(result_expr, (dict, list)):
        final = _deep_substitute(result_expr, context)
    else:
        final = result_expr

    return normalize_result(final), debug

# ---------------------------------------------------------------------------
# Tool definition & handler
# ---------------------------------------------------------------------------

TOOL_DEFINITION: Dict[str, Any] = {
    "name": "composition",
    "description": (
        "Execute a composition: an ordered sequence of tool calls with "
        "variable substitution (${.arg}, ${[N].path}, $${ escape) and "
        "nesting (steps whose tool is 'composition').  Intermediate results "
        "are normalised (content-block text extraction → JSON parse → raw "
        "fallback) before being available to subsequent steps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "composition": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "object"},
                ],
                "description": (
                    "Composition definition (JSON string or object) with "
                    "'steps' (list) and optional 'result'."
                ),
            },
            "args": {
                "type": ["object", "array"],
                "description": (
                    "Arguments available as ${.arg} (dict) or ${[N].path} "
                    "(array) inside the composition."
                ),
                "default": {},
            },
        },
        "required": ["composition"],
    },
}


async def handle_composition_tool(
    tool_args: Dict[str, Any],
    tool_execute_fn: Any,
) -> Dict[str, Any]:
    """
    Entry point invoked by the tools execute handler.

    Returns a **response envelope** — debug info lives here and
    nowhere else (no prints, no logs).
    """
    # Accept any of the common key names
    comp_source = tool_args.get(
        "composition",
        tool_args.get("source", tool_args.get("definition")),
    )
    comp_args: Union[Dict[str, Any], List[Any]] = tool_args.get("args", {})

    comp = load_composition(comp_source)
    result, debug = await execute_composition(comp, comp_args, tool_execute_fn)

    # Envelope: result always present; debug always present (may be empty
    # list for trivial compositions — consumer decides whether to show it).
    envelope: Dict[str, Any] = {
        "result": result,
        "_debug": {
            "steps": debug,
            "total_steps": len(debug),
        },
    }
    return envelope

# ---------------------------------------------------------------------------
# Registration helper for tool_aggregator
# ---------------------------------------------------------------------------


def register(tool_registry: Dict[str, Any]) -> None:
    """Plug the ``composition`` tool into *tool_registry*."""
    tool_registry["composition"] = {
        "definition": TOOL_DEFINITION,
        "handler": handle_composition_tool,
    }
