"""Structured logging wrapper for the documentary pipeline (issue #80).

This module consolidates the fragmented ``logger = logging.getLogger(...)``
usage across the pipeline into a single structured-logging layer that
emits JSON log records with consistent fields.

Design goals
------------
1. Non-invasive: we do NOT delete existing loggers.  Instead we install a
   JSON ``logging.Formatter`` on the root logger (opt-in via
   ``STRUCTURED_LOGGING=1``) so every ``logger.info("...")`` call
   throughout the codebase is automatically emitted as a JSON record.
2. Consistent fields: every record carries ``run_id``, ``stage``,
   ``scene_id``, ``duration_ms``, and ``level``.  Callers that care
   about these fields inject them via ``LogContext`` / ``stage_span``;
   everything else falls back to safe defaults.
3. Validation checkpoints (#75): ``validation_checkpoint`` records an
   agent's "did-you-produce-something-sane" assertion and emits both a
   structured log record and a dashboard SSE event.  Used by the audio
   agent to confirm N non-silent clips, by the video agent to confirm
   clip count matches scene count, etc.

Public API
----------
- ``setup_structured_logging(level: str = "INFO") -> None``
- ``LogContext`` — contextvars-backed run_id/stage/scene_id holder
- ``stage_span(stage: str, scene_id: str = "")`` — context manager
  that logs ``stage_start`` / ``stage_end`` + duration_ms
- ``validation_checkpoint(agent, check, ok, detail="", **fields)``
- ``get_logger(name: str) -> logging.Logger``
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# ---------------------------------------------------------------------------
# Context variables — thread/async-safe storage for run_id / stage / scene_id
# ---------------------------------------------------------------------------

_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pipeline_run_id", default=""
)
_stage_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pipeline_stage", default=""
)
_scene_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pipeline_scene_id", default=""
)


class LogContext:
    """Handle to the current run_id / stage / scene_id for structured logs."""

    @staticmethod
    def set_run_id(run_id: str) -> None:
        _run_id_var.set(run_id or "")

    @staticmethod
    def set_stage(stage: str) -> None:
        _stage_var.set(stage or "")

    @staticmethod
    def set_scene_id(scene_id: str) -> None:
        _scene_id_var.set(scene_id or "")

    @staticmethod
    def snapshot() -> dict[str, str]:
        return {
            "run_id": _run_id_var.get(),
            "stage": _stage_var.get(),
            "scene_id": _scene_id_var.get(),
        }


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

# Keys that LogRecord reserves; anything else in __dict__ is user extra.
_STANDARD_LOGRECORD_KEYS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime",
    }
)


class _JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON with pipeline context."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = LogContext.snapshot()
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": getattr(record, "run_id", ctx["run_id"]),
            "stage": getattr(record, "stage", ctx["stage"]),
            "scene_id": getattr(record, "scene_id", ctx["scene_id"]),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # Include any user-supplied extras (logger.info(..., extra={...}))
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_KEYS or key in payload:
                continue
            if key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            # Last-resort fallback so logging itself never raises.
            return f"{record.levelname} {record.name} {record.getMessage()}"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_SETUP_DONE = False


def setup_structured_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent.  Honours ``STRUCTURED_LOGGING=0`` as an explicit opt-out
    so existing pretty-print logs keep working during local dev.
    """
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    if os.environ.get("STRUCTURED_LOGGING", "").strip().lower() in ("0", "false", "no"):
        _SETUP_DONE = True
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Wrap (not replace) existing handlers so we don't delete anything.
    formatter = _JSONFormatter()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)

    _SETUP_DONE = True
    root.info("Structured logging enabled (#80)")


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits structured JSON when setup ran."""
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Stage span helper
# ---------------------------------------------------------------------------


@contextmanager
def stage_span(
    stage: str,
    scene_id: str = "",
    logger: Optional[logging.Logger] = None,
) -> Iterator[dict[str, Any]]:
    """Context manager that records start/end + duration_ms for a stage.

    Example:
        with stage_span("audio", scene_id="scene_003") as span:
            generate_clip(...)
            span["clips_generated"] = 5
    """
    log = logger or get_logger("pipeline.stage")
    prev_stage = _stage_var.get()
    prev_scene = _scene_id_var.get()
    _stage_var.set(stage)
    if scene_id:
        _scene_id_var.set(scene_id)

    span_data: dict[str, Any] = {}
    start = time.time()
    log.info(
        "stage_start",
        extra={"stage": stage, "scene_id": scene_id, "event": "stage_start"},
    )
    try:
        yield span_data
    except BaseException as exc:
        duration_ms = int((time.time() - start) * 1000)
        log.error(
            "stage_error",
            extra={
                "stage": stage,
                "scene_id": scene_id,
                "duration_ms": duration_ms,
                "event": "stage_error",
                "error": str(exc),
                **span_data,
            },
        )
        raise
    else:
        duration_ms = int((time.time() - start) * 1000)
        log.info(
            "stage_end",
            extra={
                "stage": stage,
                "scene_id": scene_id,
                "duration_ms": duration_ms,
                "event": "stage_end",
                **span_data,
            },
        )
    finally:
        _stage_var.set(prev_stage)
        _scene_id_var.set(prev_scene)


# ---------------------------------------------------------------------------
# Validation checkpoints (#75)
# ---------------------------------------------------------------------------


def validation_checkpoint(
    agent: str,
    check: str,
    ok: bool,
    detail: str = "",
    emit_sse: bool = True,
    **fields: Any,
) -> bool:
    """Record an agent's 'did-you-produce-something-sane' assertion.

    Parameters
    ----------
    agent : str
        The agent that ran the check (e.g. ``"audio_agent"``).
    check : str
        A short description of what's being asserted (e.g.
        ``"non_silent_clip_count_matches_scenes"``).
    ok : bool
        Whether the assertion passed.
    detail : str
        Human-readable detail (failed value, expected value, etc).
    emit_sse : bool
        Whether to also ping the dashboard /ingest endpoint.  Defaults
        to True; set to False for tests.
    **fields : Any
        Extra fields to include in the structured log record.  Common
        examples: ``expected``, ``actual``, ``scene_count``,
        ``clip_count``.

    Returns
    -------
    bool
        The value of ``ok``, so callers can chain::

            if not validation_checkpoint("audio", "clip_count", ...):
                raise RuntimeError(...)
    """
    log = get_logger("pipeline.validation")
    ctx = LogContext.snapshot()
    level = logging.INFO if ok else logging.ERROR
    log.log(
        level,
        "validation_checkpoint agent=%s check=%s ok=%s %s",
        agent, check, ok, detail,
        extra={
            "event": "validation_checkpoint",
            "agent": agent,
            "check": check,
            "ok": ok,
            "detail": detail,
            **fields,
        },
    )
    if emit_sse:
        try:
            # Lazy import so tests can exercise this module without
            # pulling the dashboard stack into scope.
            from dashboard.sse import emit_stage_event

            emit_stage_event(
                run_id=ctx["run_id"] or "unknown",
                stage=ctx["stage"] or agent,
                status="completed" if ok else "failed",
                scene_id=ctx["scene_id"],
                detail=f"{check}: {detail}" if detail else check,
            )
        except Exception as exc:  # pragma: no cover — dashboard optional
            log.debug("emit_stage_event failed: %s", exc)

    return ok
