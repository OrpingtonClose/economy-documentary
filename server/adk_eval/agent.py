"""Agent re-export for the ADK eval harness.

``adk web`` and ``adk eval`` discover agents by looking for a ``root_agent``
attribute on an ``agent`` module inside an agent directory. This module
re-exports the canonical documentary ``pipeline_agent`` without duplicating
any wiring or state, so goldens captured through the dashboard regress the
same orchestrator the production server runs.

Production code paths are untouched: importing this module triggers the same
side-effects that ``server.py`` already causes when it imports
``agents.pipeline``.
"""

from __future__ import annotations

from agents.pipeline import pipeline_agent

# ADK convention: agent.py exports ``root_agent``.
root_agent = pipeline_agent

__all__ = ["root_agent"]
