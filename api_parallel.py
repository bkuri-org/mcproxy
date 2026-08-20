"""Parallel execution support for MCProxy.

Provides concurrent tool execution with semaphore-based concurrency limiting.
All results pass through the shared apply_migration() helper from
schema_migration.py to guarantee schema consistency even when the
parallel path bypasses the router choke point.

Results are additionally passed through apply_result_limit() from
result_limiter.py to enforce a cumulative byte budget, ensuring that
oversized responses are summarised or truncated before returning to
the caller.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

from logging_config import get_logger
from server.result_limiter import apply_result_limit
from schema_migration import apply_migration

logger = get_logger(__name__)

T = TypeVar("T")

DEFAULT_MAX_CONCURRENCY: int = 5
DEFAULT_MAX_RESULT_SIZE: int = 50000


@dataclass
class ParallelResult:
    """Result from a single parallel execution."""

    status: str
    result: Any = None
    error: Optional[str] = None


class ParallelExecutor:
    """Execute multiple callables concurrently with concurrency limiting.

    Uses asyncio.Semaphore for concurrency control and asyncio.gather
    with return_exceptions=True for allSettled pattern (no fail-fast).

    Example:
        executor = ParallelExecutor(max_concurrency=5)
        results = await executor.execute_parallel([
            lambda: tool1(arg="a"),
            lambda: tool2(arg="b"),
        ])
    """

    def __init__(self, max_concurrency: int = DEFAULT_MAX_CONCURRENCY):
        """Initialize ParallelExecutor.

        Args:
            max_concurrency: Maximum number of concurrent executions
        """
        self._max_concurrency = max_concurrency

    async def execute_parallel(
        self,
        callables: List[Callable[[], Awaitable[T]]],
        max_result_size: Optional[int] = None,
    ) -> List[ParallelResult]:
        """Execute multiple async callables concurrently.

        Args:
            callables: List of async callables to execute
            max_result_size: Cumulative byte budget for each result.
                Extracted from per-call params upstream; clamped to
                DEFAULT_MAX_RESULT_SIZE when None or exceeds the default.

        Returns:
            List of ParallelResult objects in order (allSettled pattern)
        """
        if not callables:
            return []

        effective_max = (
            min(max_result_size, DEFAULT_MAX_RESULT_SIZE)
            if max_result_size is not None
            else DEFAULT_MAX_RESULT_SIZE
        )

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_with_semaphore(
            coro_func: Callable[[], Awaitable[T]],
        ) -> ParallelResult:
            async with semaphore:
                try:
                    result = await coro_func()
                    return ParallelResult(status="fulfilled", result=result)
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {str(e)}"
                    logger.debug(f"Parallel execution failed: {error_msg}")
                    return ParallelResult(status="rejected", error=error_msg)

        tasks = [run_with_semaphore(c) for c in callables]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results: List[ParallelResult] = []
        for r in results:
            if isinstance(r, ParallelResult):
                final_results.append(r)
            elif isinstance(r, Exception):
                final_results.append(
                    ParallelResult(
                        status="rejected",
                        error=f"{type(r).__name__}: {str(r)}",
                    )
                )
            else:
                final_results.append(ParallelResult(status="fulfilled", result=r))

        # Blocking-audit gate: every fulfilled result must pass through the
        # shared apply_migration() helper so that the parallel path never
        # diverges from the router's schema-migration behaviour.
        # After migration, apply_result_limit enforces the cumulative byte
        # budget propagated recursively through nested structures.
        for idx, res in enumerate(final_results):
            if res.status == "fulfilled" and res.result is not None:
                try:
                    res.result = apply_migration(res.result)
                except Exception as exc:
                    logger.warning(
                        "Schema migration failed on parallel result[%d]: %s",
                        idx,
                        exc,
                    )
                    res.status = "rejected"
                    res.error = f"schema_migration: {type(exc).__name__}: {exc}"
                    res.result = None
                    continue
                try:
                    res.result = apply_result_limit(res.result, effective_max)
                except Exception as exc:
                    logger.warning(
                        "Result limiting failed on parallel result[%d]: %s",
                        idx,
                        exc,
                    )
                    res.status = "rejected"
                    res.error = f"result_limiter: {type(exc).__name__}: {exc}"
                    res.result = None

        return final_results

    @property
    def max_concurrency(self) -> int:
        """Get the maximum concurrency limit."""
        return self._max_concurrency

    @max_concurrency.setter
    def max_concurrency(self, value: int) -> None:
        """Set the maximum concurrency limit."""
        if value < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._max_concurrency = value


def create_parallel_executor(
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> ParallelExecutor:
    """Factory function to create a ParallelExecutor.

    Args:
        max_concurrency: Maximum concurrent executions (default: 5)

    Returns:
        Configured ParallelExecutor instance
    """
    return ParallelExecutor(max_concurrency=max_concurrency)


def create_intent_classifier(
    llm_call: Callable[[str], str],
) -> Callable[[str], Any]:
    """Create an intent classifier callable backed by the given LLM.

    All intent logic (schema, prompts, validation, normalisation) lives in
    ``reasoning.intent``; this thin adapter merely injects the *llm_call*
    dependency and returns a ready-to-use ``text -> Intent`` callable.

    Args:
        llm_call: A callable that sends a prompt string to an LLM and
            returns the raw response text.

    Returns:
        A callable that accepts a user-text string and returns an
        ``Intent`` dataclass instance.
    """
    from reasoning.intent import classify_intent

    def classifier(text: str) -> Any:
        return classify_intent(text, llm_call=llm_call)

    return classifier
