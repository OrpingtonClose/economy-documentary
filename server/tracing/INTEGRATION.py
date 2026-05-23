"""Integration reference — copy these snippets into the existing pipeline.

1.  strands_agents/run_strands.py
2.  strands_agents/graph_pipeline.py
3.  server/callbacks/before_tool.py  +  after_tool.py
4.  strands_agents/hooks/pipeline_hooks.py
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 1.  run_strands.py  — initialise store and attach hook
# ═══════════════════════════════════════════════════════════════════════════════

# Near the top, add:
#     from tracing.snapshot_hooks import SnapshotHook, snapshot_vm_state, snapshot_otio_state

# Inside run_documentary(), after ``auto_tracer.start()``:
#     snapshot_hook = SnapshotHook(run_id=run_id)
#     hooks.append(snapshot_hook)

# In the finally block, before releasing the lock:
#     try:
#         from worker_provisioner import get_provisioner
#         prov = get_provisioner()
#         if prov:
#             snapshot_vm_state(run_id, prov)
#     except Exception:
#         pass
#     try:
#         snapshot_otio_state(run_id, timeline_path)
#     except Exception:
#         pass

# ═══════════════════════════════════════════════════════════════════════════════
# 2.  graph_pipeline.py  — inject SnapshotHook into the graph
# ═══════════════════════════════════════════════════════════════════════════════

# In build_documentary_graph() (or wherever hooks are registered):
#     from tracing.snapshot_hooks import SnapshotHook
#     if "snapshot_hook" not in [type(h).__name__ for h in hooks]:
#         hooks.append(SnapshotHook(run_id=state.get("_run_id", "unknown")))

# ═══════════════════════════════════════════════════════════════════════════════
# 3.  callbacks/before_tool.py  +  after_tool.py  — thin wrappers
# ═══════════════════════════════════════════════════════════════════════════════

# before_tool.py — add at the bottom of before_tool_callback(), just before ``return None``:
#     from tracing.snapshot_store import get_store
#     get_store().record_tool_call(
#         agent=agent_name, tool_name=tool_name,
#         args=dict(args) if args else {},
#         result=None, duration_ms=0.0,
#         run_id=tool_context.state.get("_run_id", "unknown"),
#     )

# after_tool.py — add at the bottom of after_tool_callback(), just before ``return None``:
#     from tracing.snapshot_store import get_store
#     get_store().record_tool_call(
#         agent=agent_name, tool_name=tool_name,
#         args=dict(args) if args else {},
#         result=tool_response,
#         duration_ms=duration * 1000.0,
#         run_id=tool_context.state.get("_run_id", "unknown"),
#     )

# ═══════════════════════════════════════════════════════════════════════════════
# 4.  strands_agents/hooks/pipeline_hooks.py  — graph transitions
# ═══════════════════════════════════════════════════════════════════════════════

# In ApprovalGateHook.on_before_node_call(), after the approval check:
#     from tracing.snapshot_store import get_store
#     get_store().record_graph_transition(
#         from_node=state.get("_last_node", "__start__"),
#         to_node=node_id,
#         reason="approval_gate_passed" if state.get(approval_key) else "approval_gate_blocked",
#         run_id=state.get("_run_id", "unknown"),
#         agent=node_id,
#     )

# In BudgetHook.on_after_node_call(), after budget check:
#     from tracing.snapshot_store import get_store
#     get_store().record_agent_decision(
#         agent="orchestrator",
#         decision_type="budget_check",
#         payload={"accrued": self._accrued, "budget": self._budget},
#         run_id=state.get("_run_id", "unknown"),
#     )

# ═══════════════════════════════════════════════════════════════════════════════
# 5.  Resume entrypoint (new helper in run_strands.py)
# ═══════════════════════════════════════════════════════════════════════════════

# Add this function for CLI resume support:
#
# async def resume_documentary(run_id: str, *, output_dir: str = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
#     from tracing.snapshot_store import get_store
#     ctx = get_store().reconstruct_state(run_id)
#     state = ctx.to_state_dict()
#     print(f"[RESUME] Reconstructed state for {run_id}: stage={ctx.current_stage}")
#     # ... feed state into graph shell.run(initial_state=state)
#     return await run_documentary(brief=state.get("brief", ""), output_dir=output_dir)
#
# In main(), detect ``--resume run_id``:
#     if sys.argv[1] == "--resume" and len(sys.argv) >= 3:
#         run_id = sys.argv[2]
#         result = asyncio.run(resume_documentary(run_id=run_id))
#         print(result)
#         return
