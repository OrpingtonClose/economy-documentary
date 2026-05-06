"""
Feature flag — switch between ADK and Strands pipelines.

The feature flag controls which pipeline runs when the server starts.
During migration, both pipelines coexist:
  - Default: ADK pipeline (unchanged, production-trusted)
  - Flag on: Strands pipeline (new, under development)

The flag is read from the ``PIPELINE_BACKEND`` environment variable:
  - ``adk`` (default) — runs the legacy ADK SequentialAgent pipeline
  - ``strands`` — runs the new Strands Graph pipeline

Shadow mode (both pipelines run, old output used) is available via
``PIPELINE_BACKEND=shadow``. This lets us validate the Strands pipeline
against real inputs without affecting production output.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flag values
# ---------------------------------------------------------------------------

BACKEND_ADK = "adk"
BACKEND_STRANDS = "strands"
BACKEND_SHADOW = "shadow"

_VALID_BACKENDS = frozenset({BACKEND_ADK, BACKEND_STRANDS, BACKEND_SHADOW})


def get_pipeline_backend() -> str:
    """Read the pipeline backend from the environment.

    Returns:
        One of 'adk', 'strands', or 'shadow'.
    """
    backend = os.environ.get("PIPELINE_BACKEND", BACKEND_ADK).lower()
    if backend not in _VALID_BACKENDS:
        logger.warning(
            "Invalid PIPELINE_BACKEND='%s', defaulting to '%s'",
            backend, BACKEND_ADK,
        )
        return BACKEND_ADK
    return backend


def is_strands_enabled() -> bool:
    """Check if the Strands pipeline is active (non-ADK)."""
    return get_pipeline_backend() in (BACKEND_STRANDS, BACKEND_SHADOW)


def is_shadow_mode() -> bool:
    """Check if shadow mode is active (both pipelines run)."""
    return get_pipeline_backend() == BACKEND_SHADOW


# ---------------------------------------------------------------------------
# Pipeline selector
# ---------------------------------------------------------------------------


async def run_pipeline(task: str, **kwargs) -> dict:
    """Run the appropriate pipeline based on the feature flag.

    In ADK mode: runs the legacy SequentialAgent pipeline.
    In Strands mode: runs the new Graph pipeline.
    In Shadow mode: runs both, returns ADK output, logs Strands differences.

    Args:
        task: The user's brief / task string.
        **kwargs: Additional pipeline parameters.

    Returns:
        The pipeline result dict.
    """
    backend = get_pipeline_backend()

    if backend == BACKEND_ADK:
        return await _run_adk_pipeline(task, **kwargs)

    if backend == BACKEND_STRANDS:
        return await _run_strands_pipeline(task, **kwargs)

    if backend == BACKEND_SHADOW:
        # Run both; return ADK output
        import asyncio
        adk_task = asyncio.create_task(_run_adk_pipeline(task, **kwargs))
        strands_task = asyncio.create_task(_run_strands_pipeline(task, **kwargs))
        adk_result = await adk_task
        try:
            strands_result = await strands_task
            logger.info("Shadow mode: both pipelines completed. Logging Strands diff.")
            _log_shadow_diff(adk_result, strands_result)
        except Exception as exc:
            logger.warning("Shadow mode: Strands pipeline failed: %s", exc)
        return adk_result

    # Fallback
    return await _run_adk_pipeline(task, **kwargs)


async def _run_adk_pipeline(task: str, **kwargs) -> dict:
    """Run the legacy ADK pipeline."""
    from agents.pipeline import build_documentary_pipeline
    pipeline = build_documentary_pipeline()
    # The ADK pipeline uses its own runner
    logger.info("Running ADK pipeline")
    # Stub — actual invocation depends on ADK Runner setup
    return {"backend": "adk", "task": task, "status": "placeholder"}


async def _run_strands_pipeline(task: str, **kwargs) -> dict:
    """Run the new Strands Graph pipeline."""
    from strands_agents.graph_pipeline import build_documentary_graph, RecoveryShell
    from strands_agents.hooks.plugin_registry import build_pipeline_hook_registry

    registry = build_pipeline_hook_registry()
    graph = build_documentary_graph(hooks=registry._hooks if hasattr(registry, '_hooks') else None)
    shell = RecoveryShell(graph)

    logger.info("Running Strands pipeline")
    result = await shell.run(task)
    return {"backend": "strands", "task": task, "result": result, "status": "completed"}


def _log_shadow_diff(adk_result: dict, strands_result: dict) -> None:
    """Log differences between ADK and Strands pipeline outputs."""
    logger.info("Shadow diff: ADK status=%s, Strands status=%s",
                adk_result.get("status"), strands_result.get("status"))
