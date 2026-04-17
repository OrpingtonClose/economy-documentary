"""ADK plugin stack and OpenTelemetry observability setup.

Provides two public helpers:

* ``setup_otel()``   -- configures OTel span exporters (SQLite archive +
  optional Phoenix dashboard).

* ``build_plugins()`` -- returns the ordered list of ADK ``BasePlugin`` instances:

  1. **ContextFilterPlugin** -- context window management
  2. **ReflectAndRetryToolPlugin** -- auto-retry failed tool calls
  3. **GlobalInstructionPlugin** -- documentary-specific global instructions
  4. **ReasoningTracePlugin** -- full LLM traces stored + surfaced to frontend

  ``ReasoningTracePlugin`` replaces the old LoggingPlugin + DebugLoggingPlugin
  + ADK_DEBUG + PHOENIX_ENABLED scattered configuration.  Full LLM
  request/response content is always stored (no env var gates) and reasoning
  chatter is pushed to the AG-UI event bus so the human observer sees what
  every agent is thinking in real time.
"""

from __future__ import annotations

import logging
import os
from typing import List

from google.adk.plugins.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

# -- OTel span archive directory -----------------------------------------------
_FINDINGS_DIR = os.environ.get(
    "FINDINGS_DIR",
    os.path.join(os.path.expanduser("~"), ".documentary-pipeline"),
)
_SPANS_DB = os.path.join(_FINDINGS_DIR, "adk_spans.db")

# -- Phoenix configuration -----------------------------------------------------
_PHOENIX_ENABLED = os.environ.get("PHOENIX_ENABLED", "").strip() == "1"
_PHOENIX_ENDPOINT = os.environ.get(
    "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"
)


def setup_otel() -> None:
    """Configure dual OTel span exporters for ADK's built-in tracing.

    Exporter 1 -- SqliteSpanExporter (always on).
    Exporter 2 -- Phoenix OTLP (when ``PHOENIX_ENABLED=1``).
    """
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    from google.adk.telemetry.setup import OTelHooks, maybe_set_otel_providers
    from google.adk.telemetry.sqlite_span_exporter import SqliteSpanExporter

    os.makedirs(_FINDINGS_DIR, exist_ok=True)

    processors = []

    # 1. SQLite archive -- always on
    sqlite_exporter = SqliteSpanExporter(db_path=_SPANS_DB)
    processors.append(SimpleSpanProcessor(sqlite_exporter))
    logger.info("OTel: SQLite span archive -> %s", _SPANS_DB)

    # 2. Phoenix OTLP -- opt-in via PHOENIX_ENABLED=1
    if _PHOENIX_ENABLED:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            phoenix_exporter = OTLPSpanExporter(endpoint=_PHOENIX_ENDPOINT)
            processors.append(BatchSpanProcessor(phoenix_exporter))
            logger.info("OTel: Phoenix OTLP -> %s", _PHOENIX_ENDPOINT)
        except ImportError:
            logger.warning(
                "OTel: Phoenix requested but opentelemetry-exporter-otlp "
                "not installed -- skipping"
            )
    else:
        logger.info("OTel: Phoenix disabled (set PHOENIX_ENABLED=1 to enable)")

    hooks = OTelHooks(span_processors=processors)
    maybe_set_otel_providers([hooks])
    logger.info("OTel: %d span processor(s) registered", len(processors))


# -- Global instruction for all agents -----------------------------------------
_GLOBAL_INSTRUCTION = """\
DOCUMENTARY PIPELINE RULES (apply to every agent):
1. All timeline operations MUST go through the OTIO tools. Never manipulate
   timeline files directly.
2. Every video clip MUST be at least as long as its corresponding audio.
   Use the 15% margin rule: generate video at target_duration * 1.15.
3. LoRA selections are creative decisions. The Content Analyst reads the
   narration semantics and queries the LoRA catalog to choose styles that
   deeply connect to the narrative content.
4. Never create duplicate clips. Before adding any clip to the timeline,
   check if one with the same scene_num + index already exists.
5. All subprocess calls use list form (no shell=True).
6. bf16 only for video generation. No FP8, no quantization.
"""


def build_plugins() -> List[BasePlugin]:
    """Return the ordered list of ADK plugins for every Runner / App.

    Observability is handled entirely by ``ReasoningTracePlugin`` — it stores
    full LLM request/response content in SQLite and pushes reasoning chatter
    to the frontend via the AG-UI event bus.  No env vars required.
    """
    from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
    from google.adk.plugins.global_instruction_plugin import GlobalInstructionPlugin
    from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin

    from plugins.reasoning_trace import ReasoningTracePlugin

    plugins: List[BasePlugin] = [
        # 1. Context management
        ContextFilterPlugin(
            num_invocations_to_keep=int(
                os.environ.get("CONTEXT_INVOCATIONS_TO_KEEP", "2")
            ),
        ),
        # 2. Self-healing tools
        ReflectAndRetryToolPlugin(
            max_retries=int(os.environ.get("TOOL_MAX_RETRIES", "2")),
            throw_exception_if_retry_exceeded=False,
        ),
        # 3. Cross-cutting instructions
        GlobalInstructionPlugin(global_instruction=_GLOBAL_INSTRUCTION),
        # 4. Full observability — stores traces + surfaces reasoning to UI
        ReasoningTracePlugin(),
    ]

    logger.info(
        "ADK plugins: %s",
        ", ".join(p.name for p in plugins),
    )
    return plugins
