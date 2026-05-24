"""Integration hooks that wire SnapshotStore into the Strands pipeline.

Drop-in HookProvider for the documentary graph.  Also provides thin
wrapper helpers for the legacy ADK callback style (before_tool / after_tool).

Usage — Strands graph (preferred)::

    from tracing.snapshot_hooks import SnapshotHook
    graph.add_hook_provider(SnapshotHook(run_id=run_id))

Usage — legacy ADK callbacks::

    from tracing.snapshot_hooks import wrap_before_tool, wrap_after_tool
    before_tool_callback = wrap_before_tool(before_tool_callback, run_id)
    after_tool_callback  = wrap_after_tool(after_tool_callback, run_id)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from strands.hooks import (
    AfterModelCallEvent,
    AfterNodeCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeNodeCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from tracing.snapshot_store import SnapshotStore, get_store

logger = logging.getLogger(__name__)


class SnapshotHook(HookProvider):
    """Strands HookProvider that records every significant lifecycle event.

    Attach to a Graph via ``graph.add_hook_provider(SnapshotHook(run_id))``.
    All writes are synchronous and commit immediately.
    """

    def __init__(
        self,
        run_id: str,
        store: Optional[SnapshotStore] = None,
    ) -> None:
        self.run_id = run_id
        self.store = store or get_store()
        self._tool_starts: dict[str, float] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeNodeCallEvent, self.on_before_node_call)
        registry.add_callback(AfterNodeCallEvent, self.on_after_node_call)
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)
        registry.add_callback(BeforeModelCallEvent, self.on_before_model_call)
        registry.add_callback(AfterModelCallEvent, self.on_after_model_call)

    # ------------------------------------------------------------------
    # Node / graph transitions
    # ------------------------------------------------------------------
    async def on_before_node_call(self, event: BeforeNodeCallEvent) -> None:
        node_id = getattr(event, "node_id", "unknown")
        state = getattr(event, "invocation_state", {}) or {}
        prev = state.get("_last_node", "__start__")
        self.store.record_graph_transition(
            from_node=prev,
            to_node=node_id,
            reason="node_start",
            run_id=self.run_id,
            agent=node_id,
        )

    async def on_after_node_call(self, event: AfterNodeCallEvent) -> None:
        node_id = getattr(event, "node_id", "unknown")
        state = getattr(event, "invocation_state", {}) or {}
        if state:
            state["_last_node"] = node_id
        # Opportunistic file-state capture after every node
        try:
            from tools.otio_file_ops import resolve_timeline_path
            tp = resolve_timeline_path()
            pipeline_dir = os.path.dirname(os.path.dirname(tp))
        except Exception:
            pipeline_dir = "/tmp/documentary-pipeline"
        files = _scan_artifacts(pipeline_dir)
        if files:
            self.store.record_file_state(
                files_json=files,
                run_id=self.run_id,
                agent=node_id,
            )

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------
    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_name = getattr(event, "tool_name", "unknown")
        call_id = getattr(event, "call_id", tool_name)
        self._tool_starts[call_id] = time.time()

    async def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_name = getattr(event, "tool_name", "unknown")
        call_id = getattr(event, "call_id", tool_name)
        start = self._tool_starts.pop(call_id, 0.0)
        duration_ms = (time.time() - start) * 1000.0 if start else 0.0

        args: dict[str, Any] = {}
        result: Any = None
        if hasattr(event, "tool_use") and isinstance(event.tool_use, dict):
            args = dict(event.tool_use.get("args", {}))
        if hasattr(event, "result"):
            result = event.result
        if hasattr(event, "exception") and event.exception:
            result = {"error": str(event.exception)}

        agent = getattr(event, "agent_name", "pipeline")
        self.store.record_tool_call(
            agent=agent,
            tool_name=tool_name,
            args=args,
            result=result,
            duration_ms=duration_ms,
            run_id=self.run_id,
        )

    # ------------------------------------------------------------------
    # LLM turns
    # ------------------------------------------------------------------
    async def on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        model = "unknown"
        messages: list[dict[str, Any]] = []
        params: dict[str, Any] = {}

        raw_model = getattr(event, "model", None)
        if raw_model is not None:
            model = str(raw_model)
        raw_messages = getattr(event, "messages", None)
        if isinstance(raw_messages, list):
            messages = [dict(m) if not isinstance(m, dict) else m for m in raw_messages]
        raw_params = getattr(event, "params", None)
        if isinstance(raw_params, dict):
            params = dict(raw_params)

        agent = getattr(event, "agent_name", "pipeline")
        self.store.record_llm_request(
            agent=agent,
            messages=messages,
            model=model,
            params=params,
            run_id=self.run_id,
        )

    async def on_after_model_call(self, event: AfterModelCallEvent) -> None:
        response_text = ""
        usage: dict[str, Any] = {}
        duration_ms = 0.0

        resp = getattr(event, "response", None)
        if resp is not None:
            raw_content = getattr(resp, "content", None)
            if raw_content is not None:
                response_text = str(raw_content)
            elif isinstance(resp, dict):
                response_text = str(resp.get("content", resp))
            raw_usage = getattr(resp, "usage", None)
            if raw_usage is not None:
                try:
                    usage = dict(raw_usage)
                except Exception:
                    usage = {"raw": str(raw_usage)}
        raw_duration = getattr(event, "duration_ms", None)
        if raw_duration is not None:
            duration_ms = float(raw_duration)

        agent = getattr(event, "agent_name", "pipeline")
        self.store.record_llm_response(
            agent=agent,
            response_text=response_text,
            usage=usage,
            duration_ms=duration_ms,
            run_id=self.run_id,
        )


# ------------------------------------------------------------------
# Legacy ADK-style callback wrappers
# ------------------------------------------------------------------

def wrap_before_tool(
    original: Callable[..., Optional[dict[str, Any]]],
    run_id: str,
    store: Optional[SnapshotStore] = None,
) -> Callable[..., Optional[dict[str, Any]]]:
    """Wrap an existing before_tool_callback so it also snapshots state."""
    _store = store or get_store()
    _starts: dict[str, float] = {}

    def wrapped(tool: Any, args: dict[str, Any], tool_context: Any) -> Optional[dict[str, Any]]:
        tool_name = tool.name if hasattr(tool, "name") else str(tool)
        call_id = getattr(tool_context, "function_call_id", "") or tool_name
        _starts[call_id] = time.time()
        return original(tool, args, tool_context)

    return wrapped


def wrap_after_tool(
    original: Callable[..., Optional[dict[str, Any]]],
    run_id: str,
    store: Optional[SnapshotStore] = None,
) -> Callable[..., Optional[dict[str, Any]]]:
    """Wrap an existing after_tool_callback so it also records the snapshot."""
    _store = store or get_store()
    _starts: dict[str, float] = {}

    def wrapped(
        tool: Any,
        args: dict[str, Any],
        tool_context: Any,
        tool_response: Any,
    ) -> Optional[dict[str, Any]]:
        tool_name = tool.name if hasattr(tool, "name") else str(tool)
        start_time = getattr(tool_context, "_snapshot_start", 0.0)
        if not start_time:
            start_time = time.time() - 0.001
        duration_ms = (time.time() - start_time) * 1000.0

        agent = getattr(tool_context, "agent_name", "pipeline")
        _store.record_tool_call(
            agent=agent,
            tool_name=tool_name,
            args=dict(args) if args else {},
            result=tool_response,
            duration_ms=duration_ms,
            run_id=run_id,
        )
        return original(tool, args, tool_context, tool_response)

    return wrapped


# ------------------------------------------------------------------
# VM state capture helpers (call from fleet coordinator / provisioner)
# ------------------------------------------------------------------

def snapshot_vm_state(
    run_id: str,
    provisioner: Any,
    store: Optional[SnapshotStore] = None,
) -> int:
    """Capture current VM fleet state into the snapshot store.

    Args:
        run_id: Pipeline run id.
        provisioner: Anything with ``list_active_vms()`` or ``vms`` dict.
        store: Optional SnapshotStore instance.

    Returns:
        snapshot_id of the inserted record.
    """
    _store = store or get_store()
    vms: dict[str, Any] = {}

    if hasattr(provisioner, "list_active_vms"):
        try:
            vms = provisioner.list_active_vms()
        except Exception as exc:
            vms = {"error": str(exc)}
    elif hasattr(provisioner, "vms"):
        vms = dict(provisioner.vms)
    else:
        vms = {"raw": str(provisioner)}

    return _store.record_vm_state(
        vms_json=vms,
        run_id=run_id,
    )


# ------------------------------------------------------------------
# OTIO state capture helpers (call from otio callbacks)
# ------------------------------------------------------------------

def snapshot_otio_state(
    run_id: str,
    timeline_path: str,
    store: Optional[SnapshotStore] = None,
) -> int:
    """Read an OTIO file and store its full JSON representation.

    Returns 0 if the file is missing or unreadable.
    """
    _store = store or get_store()
    if not os.path.exists(timeline_path):
        return 0
    try:
        import opentimelineio as otio
        timeline = otio.adapters.read_from_file(timeline_path)
        # Best-effort JSON serialisation
        try:
            otio_json = _otio_to_dict(timeline)
        except Exception:
            otio_json = {"timeline_name": timeline.name, "error": "non-serialisable"}
        return _store.record_otio_state(
            otio_json=otio_json,
            run_id=run_id,
        )
    except Exception as exc:
        logger.warning("snapshot_otio_state failed: %s", exc)
        return 0


# ------------------------------------------------------------------
# Resume helpers
# ------------------------------------------------------------------

def resume_from_snapshot(
    run_id: str,
    store: Optional[SnapshotStore] = None,
) -> Optional[dict[str, Any]]:
    """Rebuild a state dict suitable for seeding a new graph invocation.

    Returns None when no snapshots exist for the run.
    """
    _store = store or get_store()
    ctx = _store.reconstruct_state(run_id)
    if not ctx.graph_history and not ctx.current_stage:
        return None
    return ctx.to_state_dict()


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _scan_artifacts(pipeline_dir: str) -> dict[str, Any]:
    """Return a map of file paths -> sizes under the pipeline directory."""
    files: dict[str, Any] = {}
    for subdir in ("timelines", "renders", "audio", "video", "checkpoints"):
        root = os.path.join(pipeline_dir, subdir)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    files[fpath] = {"size": os.path.getsize(fpath), "mtime": os.path.getmtime(fpath)}
                except OSError:
                    pass
    return files


def _otio_to_dict(timeline: Any) -> dict[str, Any]:
    """Best-effort conversion of an OTIO timeline to a plain dict."""

    def _clip_to_dict(clip: Any) -> dict[str, Any]:
        return {
            "name": getattr(clip, "name", "unknown"),
            "kind": type(clip).__name__,
            "source_range": str(getattr(clip, "source_range", None)),
            "duration": str(getattr(clip, "duration", None)),
        }

    tracks: list[dict[str, Any]] = []
    for track in getattr(timeline, "tracks", []):
        clips = []
        for child in getattr(track, "children", []):
            clips.append(_clip_to_dict(child))
        tracks.append({
            "name": getattr(track, "name", "unknown"),
            "kind": getattr(track, "kind", "unknown"),
            "clips": clips,
        })

    return {
        "name": getattr(timeline, "name", "unknown"),
        "tracks": tracks,
        "metadata": dict(getattr(timeline, "metadata", {})),
    }
