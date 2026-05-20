#!/usr/bin/env python3
"""Generate worker_defs/agents.py with dependency annotations."""

# Each tuple: (name, desc, domain, traversal, sys, task, depends_on, target_file)
WORKERS = [
    # ═══════════════════════════════════════════════════════════════════════════
    # DOMAIN 1 — OTIO Refresh
    # ═══════════════════════════════════════════════════════════════════════════
    ("otio-read-hotspot-read",
     "Maps the stale-read risk in OTIOStateManager.read()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-read hotspot. Scope: OTIOStateManager.read() only. When read() returns stale data, the LLM plans scenes based on fiction — wrong clips, wrong durations, wrong decisions. This directly corrupts the movie.",
     "Use graphify and filesystem tools to examine OTIOStateManager.read(). Read otio_manager.py. Document: exact file:line, what _timeline fields are read, what corruption would occur if the cache is stale, and what the movie would look like after this corruption. Save to workspace as otio_read_hotspot_read.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-read-hotspot-getmeta",
     "Maps the stale-read risk in OTIOStateManager.get_pipeline_metadata()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-read hotspot. Scope: OTIOStateManager.get_pipeline_metadata() only. When QA metadata is read from stale cache, gates make wrong decisions — passing bad scenes or failing good ones. The movie ships with quality defects or never completes.",
     "Use graphify and filesystem tools to examine OTIOStateManager.get_pipeline_metadata(). Read otio_manager.py. Document: exact file:line, what metadata is read, what gate decision would be wrong if stale, and how this kills the movie. Save to workspace as otio_read_hotspot_getmeta.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-read-hotspot-checkpoint",
     "Maps the stale-read risk in OTIOStateManager.checkpoint()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-read hotspot. Scope: OTIOStateManager.checkpoint() only. When checkpoint() serializes stale cache to B2, recovery restores a corrupted timeline — making resume useless or worse than useless. The movie restarts from corrupted state.",
     "Use graphify and filesystem tools to examine OTIOStateManager.checkpoint(). Read otio_manager.py. Document: exact file:line, what is serialized, what corruption would be saved to B2, and why this makes resume dangerous. Save to workspace as otio_read_hotspot_checkpoint.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-read-hotspot-clipcounts",
     "Maps the stale-read risk in OTIOStateManager._clip_counts()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-read hotspot. Scope: OTIOStateManager._clip_counts() only. When clip counts are read from stale cache, progress reports lie — the pipeline thinks 20 scenes are done when only 15 exist in memory. Budget calculations and completion detection fail.",
     "Use graphify and filesystem tools to examine OTIOStateManager._clip_counts(). Read otio_manager.py. Document: exact file:line, what counts are computed, how stale counts mislead progress tracking, and why the movie never reports completion correctly. Save to workspace as otio_read_hotspot_clipcounts.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-write-hotspot-addclip",
     "Maps the stale-write risk in OTIOStateManager.add_clip()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-write hotspot. Scope: OTIOStateManager.add_clip() only. When add_clip() starts from stale cache, it overwrites the disk file with old state + one new clip — silently dropping all scenes added since the last refresh. After scene 20, entire scenes vanish.",
     "Use graphify and filesystem tools to examine OTIOStateManager.add_clip(). Read otio_manager.py. Document: exact file:line, how the mutation works, what scenes would be silently deleted if the cache is stale, and the cascading failure mode. Save to workspace as otio_write_hotspot_addclip.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-write-hotspot-setmeta",
     "Maps the stale-write risk in OTIOStateManager.set_pipeline_metadata()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-write hotspot. Scope: OTIOStateManager.set_pipeline_metadata() only. When metadata is set on stale cache, QA verdicts and stage provenance are written to an old copy of the timeline and lost on the next refresh. The movie's quality tracking evaporates.",
     "Use graphify and filesystem tools to examine OTIOStateManager.set_pipeline_metadata(). Read otio_manager.py. Document: exact file:line, what metadata is mutated, how stale-cache mutation causes data loss, and why QA gates become unreliable. Save to workspace as otio_write_hotspot_setmeta.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-write-hotspot-writeline",
     "Maps the stale-write risk in OTIOStateManager._write_timeline()",
     "otio", "pass1_otio_refresh.md",
     "You are a code archaeologist mapping ONE stale-write hotspot. Scope: OTIOStateManager._write_timeline() only. This is the atomic write gateway. If it writes stale data, the corruption becomes permanent on disk. The movie file is permanently corrupted.",
     "Use graphify and filesystem tools to examine OTIOStateManager._write_timeline(). Read otio_manager.py. Document: exact file:line, how the atomic write works, why atomicity doesn't help with stale data, and the permanent corruption scenario. Save to workspace as otio_write_hotspot_writeline.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-method-signature",
     "Writes the def line and docstring for refresh_from_disk() in OTIOStateManager.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer writing ONE function signature. Scope: OTIOStateManager refresh_from_disk() def line only. This is the contract that all refresh wires will call.",
     "Read otio_manager.py. Write the def refresh_from_disk(self) -> None: line and a complete docstring explaining: what it does, why it exists (prevents stale-read corruption), when it should be called (before every read/mutation), and thread-safety guarantee (uses self._lock). Insert after __init__() and before the state property. Provide exact code + insertion point. Save to workspace as otio_refresh_method_signature.md.",
     [],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-method-body",
     "Implements the core body of refresh_from_disk(): path guards, disk read, and assignment.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer implementing the core logic body. Scope: refresh_from_disk() method body only (excluding error handling). This is the actual reload logic that reads from disk and updates the cache.",
     "Read otio_manager.py. Implement the body of refresh_from_disk() inside with self._lock: (1) if not self._timeline_path: return, (2) if not os.path.exists(self._timeline_path): return, (3) fresh = otio.adapters.read_from_file(self._timeline_path), (4) self._timeline = fresh. Provide exact code + insertion point. Save to workspace as otio_refresh_method_body.md.",
     ["otio-refresh-method-signature"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-method-error",
     "Wraps the refresh_from_disk() body in try/except so disk read failures are logged, not raised.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding error handling. Scope: refresh_from_disk() exception handling only. If otio.adapters.read_from_file() raises, the method must log and return — never propagate an exception that would crash the pipeline.",
     "Read otio_manager.py. Wrap the refresh_from_disk() body in try/except Exception: log the error with logger.error() and return silently. This ensures a corrupted .otio file doesn't kill the entire movie render. Provide exact before/after diff. Save to workspace as otio_refresh_method_error.md.",
     ["otio-refresh-method-body"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-read-wire",
     "Ensures the LLM sees the true timeline before making scene decisions.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager.read() only. If read() returns stale data, the LLM plans scenes based on a fiction. This is how a 20-scene movie gradually drifts into incoherence.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of read() after the docstring. This ensures every LLM query about the timeline sees the on-disk truth, not a ghost. Provide exact file:line, before/after snippet, and rationale about movie coherence. Save to workspace as otio_refresh_read_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-addclip-wire",
     "Prevents add_clip() from silently deleting scenes added by other writers.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager.add_clip() only. Without this wire, scene 21's add_clip() overwrites the disk with a 20-scene timeline, silently deleting scene 21. The movie loses scenes permanently.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of add_clip() after the docstring, before guard_mutation(). This prevents the silent scene-deletion bug. Provide exact file:line, before/after snippet, and rationale about scene preservation. Save to workspace as otio_refresh_addclip_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-setmeta-wire",
     "Prevents set_pipeline_metadata() from overwriting good metadata with stale provenance.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager.set_pipeline_metadata() only. Without this wire, QA verdicts written to stale cache are lost when the next reader refreshes from disk. The movie's quality history evaporates.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of set_pipeline_metadata() after the docstring. This ensures metadata mutations apply to the current disk state. Provide exact file:line, before/after snippet, and rationale about QA persistence. Save to workspace as otio_refresh_setmeta_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-getmeta-wire",
     "Ensures get_pipeline_metadata() returns the true QA state for gate decisions.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager.get_pipeline_metadata() only. Without this wire, gates read old QA verdicts and make wrong pass/fail decisions. Bad scenes pass, good scenes fail, and the movie quality degrades.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of get_pipeline_metadata() after the docstring. This ensures every gate decision is based on the true QA state from disk. Provide exact file:line, before/after snippet, and rationale about gate accuracy. Save to workspace as otio_refresh_getmeta_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-checkpoint-wire",
     "Prevents checkpoints from saving stale timeline state to B2.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager.checkpoint() only. Without this wire, B2 checkpoints contain ghost timelines. Resume from checkpoint restores corruption, making the resume feature actively harmful.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of checkpoint() after the docstring, before with self._lock. This ensures every B2 snapshot contains the true on-disk state. Provide exact file:line, before/after snippet, and rationale about checkpoint integrity. Save to workspace as otio_refresh_checkpoint_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-clipcounts-wire",
     "Ensures _clip_counts() returns the true scene count for progress tracking.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager._clip_counts() only. Without this wire, progress reports show 20 scenes when the cache only has 15. The pipeline declares completion prematurely or budgets incorrectly.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of _clip_counts(). This ensures every progress report and budget calculation uses the true scene count. Provide exact file:line, before/after snippet, and rationale about progress accuracy. Save to workspace as otio_refresh_clipcounts_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    ("otio-refresh-writeline-wire",
     "Prevents _write_timeline() from clobbering on-disk reality with stale cache.",
     "otio", "pass1_otio_refresh.md",
     "You are a systems engineer adding ONE function call. Scope: OTIOStateManager._write_timeline() only. This is the atomic write gateway. Without this wire, the permanent movie file is corrupted every time an external writer has modified the disk.",
     "Read otio_manager.py. Add self.refresh_from_disk() as the first line of _write_timeline(). This ensures the atomic write operation always serializes the true on-disk state, never a ghost. Provide exact file:line, before/after snippet, and rationale about file integrity. Save to workspace as otio_refresh_writeline_wire.md.",
     ["otio-refresh-method-error"],
     "server/strands_agents/otio_manager.py"),

    # ═══════════════════════════════════════════════════════════════════════════
    # DOMAIN 2 — Checkpoint/Resume
    # ═══════════════════════════════════════════════════════════════════════════
    ("checkpoint-otio-sig",
     "Designs the otio_checkpoint() function signature and contract.",
     "resume", "pass2_resume.md",
     "You are a systems architect designing ONE function signature. Scope: otio_checkpoint() only. Without a clean save contract, the pipeline cannot persist state between stages. No implementation.",
     "Read otio_file_ops.py and b2_checkpoint.py. Design the complete signature with docstring for otio_checkpoint(timeline_path, label, run_id). Include parameter types, return type, preconditions, postconditions, and error behavior. Save to workspace as checkpoint_otio_sig.md.",
     [],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-restore-sig",
     "Designs the restore_from_checkpoint() function signature and contract.",
     "resume", "pass2_resume.md",
     "You are a systems architect designing ONE function signature. Scope: restore_from_checkpoint() only. Without a clean restore contract, a crash at hour 9 means starting from scene 1. No implementation.",
     "Read otio_file_ops.py. Design the complete signature with docstring for restore_from_checkpoint(run_id, label, timeline_dir). Include parameter types, return type, how it picks the latest checkpoint, and error behavior when no checkpoint exists. Save to workspace as checkpoint_restore_sig.md.",
     [],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-list-sig",
     "Designs the list_checkpoints() function signature and contract.",
     "resume", "pass2_resume.md",
     "You are a systems architect designing ONE function signature. Scope: list_checkpoints() only. Without discovery, resume cannot know which stages completed before the crash. No implementation.",
     "Read otio_file_ops.py. Design the complete signature with docstring for list_checkpoints(timeline_dir, run_id). Include parameter types, return type, sort order, and filtering behavior. Save to workspace as checkpoint_list_sig.md.",
     [],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-dir-designer",
     "Designs the checkpoint directory layout and metadata schema.",
     "resume", "pass2_resume.md",
     "You are a systems architect designing the storage layout. Without a predictable layout, resume cannot find previous states. No code.",
     "Design the checkpoint directory structure and metadata JSON schema. Include: directory path, file naming convention, all metadata fields (run_id, label, timestamp, clip_counts, qa_summary, cost_accrued, completed), and how each field enables resume decisions. Save to workspace as checkpoint_dir_design.md.",
     [],
     "server/strands_agents/graph_pipeline.py"),

    ("checkpoint-dir-creation",
     "Implements the .checkpoints/ directory creation inside otio_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: directory creation. Scope: otio_checkpoint() mkdir logic only. Without a directory to hold snapshots, checkpoints have nowhere to live.",
     "Read otio_file_ops.py. Implement the directory creation logic for otio_checkpoint: compute checkpoint_dir from timeline_path, create .checkpoints/ with os.makedirs(exist_ok=True). Provide exact code snippet and insertion point. Save to workspace as checkpoint_dir_creation.md.",
     ["checkpoint-dir-designer", "checkpoint-otio-sig"],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-path-compute",
     "Computes the timestamped checkpoint path inside otio_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: path computation. Scope: otio_checkpoint() path string construction only. Without the correct path, the snapshot is written to the wrong location.",
     "Read otio_file_ops.py. Implement the path computation for otio_checkpoint: compute timestamp from time.time(), build suffix as {run_id}_{label}_{timestamp}, construct snapshot_path and meta_path in .checkpoints/ directory. Provide exact code snippet and insertion point. Save to workspace as checkpoint_path_compute.md.",
     ["checkpoint-dir-creation"],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-otio-copy",
     "Copies the current timeline to the snapshot path inside otio_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: file copy. Scope: otio_checkpoint() otio_write() call only. Without copying the .otio file, the checkpoint contains no timeline state.",
     "Read otio_file_ops.py. Implement the snapshot copy for otio_checkpoint: read current timeline via otio_read(timeline_path), write via otio_write(snapshot_path, timeline). Provide exact code snippet and insertion point. Save to workspace as checkpoint_otio_copy.md.",
     ["checkpoint-path-compute"],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-meta-clipcounts",
     "Implements clip-count extraction from timeline tracks inside otio_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: clip count computation. Scope: otio_checkpoint() clip counting only. Without clip counts, resume cannot verify stage progress.",
     "Read otio_file_ops.py. Implement the clip count extraction for otio_checkpoint: iterate timeline.tracks, sum isinstance(item, otio.schema.Clip) per track, store in a dict keyed by track name. Provide exact code snippet and insertion point. Save to workspace as checkpoint_meta_clipcounts.md.",
     ["checkpoint-otio-copy"],
     "server/tools/otio_file_ops.py"),

    ("checkpoint-meta-sidecar",
     "Implements sidecar dict construction and .meta.json write inside otio_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: sidecar serialization. Scope: otio_checkpoint() JSON write only. Without the sidecar file, resume cannot read checkpoint metadata.",
     "Read otio_file_ops.py. Implement the sidecar dict construction and JSON write for otio_checkpoint: build dict with run_id, label, timestamp, clip_counts, qa_summary, cost_accrued, completed=False. Write to .meta.json using json.dump(). Provide exact code snippet and insertion point. Save to workspace as checkpoint_meta_sidecar.md.",
     ["checkpoint-meta-clipcounts"],
     "server/tools/otio_file_ops.py"),

    ("restore-list-checkpoints",
     "Implements listing matching checkpoints inside restore_from_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: discovery. Scope: restore_from_checkpoint() listing only. Without finding matching checkpoints, restore has nothing to restore from.",
     "Read otio_file_ops.py. Implement the listing logic for restore_from_checkpoint: scan .checkpoints/ for files matching {run_id}_{label}_*.otio, return matching paths. Provide exact code snippet and insertion point. Save to workspace as restore_list_checkpoints.md.",
     ["checkpoint-meta-sidecar", "checkpoint-restore-sig"],
     "server/tools/otio_file_ops.py"),

    ("restore-pick-latest",
     "Implements picking the latest checkpoint by timestamp inside restore_from_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: selection. Scope: restore_from_checkpoint() timestamp comparison only. Without picking the latest, restore might use an hours-old checkpoint.",
     "Read otio_file_ops.py. Implement the selection logic for restore_from_checkpoint: parse timestamps from checkpoint filenames, pick the latest, return the selected path. Provide exact code snippet and insertion point. Save to workspace as restore_pick_latest.md.",
     ["restore-list-checkpoints"],
     "server/tools/otio_file_ops.py"),

    ("restore-copy-timeline",
     "Implements copying the selected checkpoint to timeline.otio inside restore_from_checkpoint().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: restoration. Scope: restore_from_checkpoint() file copy only. Without copying to timeline.otio, the pipeline has no working timeline.",
     "Read otio_file_ops.py. Implement the copy logic for restore_from_checkpoint: copy selected snapshot to {timeline_dir}/timeline.otio using shutil.copy2() or equivalent, return restored path. Raise FileNotFoundError if no checkpoint matches. Provide exact code snippet and insertion point. Save to workspace as restore_copy_timeline.md.",
     ["restore-pick-latest"],
     "server/tools/otio_file_ops.py"),

    ("list-checkpoints-scan",
     "Implements scanning .checkpoints/ for metadata files inside list_checkpoints().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: scanning. Scope: list_checkpoints() directory scan only. Without scanning, resume cannot discover what stages are done.",
     "Read otio_file_ops.py. Implement the scan logic for list_checkpoints: use glob or os.listdir to find all .meta.json files in .checkpoints/, parse each into a dict. Provide exact code snippet and insertion point. Save to workspace as list_checkpoints_scan.md.",
     ["restore-copy-timeline", "checkpoint-list-sig"],
     "server/tools/otio_file_ops.py"),

    ("list-checkpoints-filter",
     "Implements filtering checkpoints by run_id inside list_checkpoints().",
     "resume", "pass2_resume.md",
     "You are a systems engineer implementing ONE sub-step: filtering. Scope: list_checkpoints() run_id filter only. Without filtering, resume sees checkpoints from other movie runs.",
     "Read otio_file_ops.py. Implement the filter logic for list_checkpoints: if run_id is provided, keep only checkpoints whose run_id matches, sort by timestamp descending. Provide exact code snippet and insertion point. Save to workspace as list_checkpoints_filter.md.",
     ["list-checkpoints-scan"],
     "server/tools/otio_file_ops.py"),

    ("scenario-checkpoint-wire",
     "Saves a checkpoint after the Scenario stage so narrative planning is never lost.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: _build_scenario_agent() only. If the scenario stage completes but no checkpoint is saved, a crash means re-planning all 20 scenes from scratch.",
     "Read graph_pipeline.py. In _build_scenario_agent(), add a scenario_checkpoint tool that calls otio_checkpoint(tp, 'scenario', run_id) after the stage completes. Provide exact file:line and diff. Save to workspace as scenario_checkpoint_wire.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/graph_pipeline.py"),

    ("audio-checkpoint-wire",
     "Saves a checkpoint after the Audio stage so narration work is never lost.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: _build_audio_agent() only. If the audio stage completes but no checkpoint is saved, a crash means re-generating all TTS from scene 1.",
     "Read graph_pipeline.py. In _build_audio_agent(), add an audio_checkpoint tool that calls otio_checkpoint(tp, 'audio', run_id) after the stage completes. Provide exact file:line and diff. Save to workspace as audio_checkpoint_wire.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/graph_pipeline.py"),

    ("video-checkpoint-wire",
     "Saves a checkpoint after the Video stage so rendered clips are never lost.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: _build_video_agent() only. If the video stage completes but no checkpoint is saved, a crash means re-rendering all clips from scene 1 — the most expensive stage.",
     "Read graph_pipeline.py. In _build_video_agent(), add a video_checkpoint tool that calls otio_checkpoint(tp, 'video', run_id) after the stage completes. Provide exact file:line and diff. Save to workspace as video_checkpoint_wire.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/graph_pipeline.py"),

    ("assembly-checkpoint-wire",
     "Saves a checkpoint after the Assembly stage so the final movie state is captured.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: _build_assembly_agent() only. Without a final checkpoint, the completed movie metadata is lost on crash.",
     "Read graph_pipeline.py. In _build_assembly_agent(), add an assembly_checkpoint tool that calls otio_checkpoint(tp, 'assembly', run_id) after the stage completes. Provide exact file:line and diff. Save to workspace as assembly_checkpoint_wire.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-argparse",
     "Adds --resume-from-checkpoint CLI argument to run.py.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE CLI argument. Scope: run.py argparse only. Without this argument, the user has no way to request a resume from the command line.",
     "Read run.py. Add --resume-from-checkpoint as a CLI argument using argparse. Pass the parsed value into the pipeline startup. Provide exact file:line and diff. Save to workspace as resume_argparse.md.",
     [],
     "server/strands_agents/run.py"),

    ("resume-envvar",
     "Adds RESUME_FROM_CHECKPOINT environment variable fallback to run.py.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE env var fallback. Scope: run.py environment reading only. Without this fallback, automated deployments cannot trigger resume.",
     "Read run.py. Add RESUME_FROM_CHECKPOINT environment variable as a fallback to the CLI argument. If the CLI arg is not provided, read from os.environ. Provide exact file:line and diff. Save to workspace as resume_envvar.md.",
     ["resume-argparse"],
     "server/strands_agents/run.py"),

    ("resume-shell-store",
     "Stores the resume flag in RecoveryShell for access during graph execution.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE instance variable. Scope: RecoveryShell.__init__() only. Without storing the flag, the resume logic has no state to act on.",
     "Read graph_pipeline.py (RecoveryShell). In __init__(), add self._resume_from_checkpoint parameter storage. Provide exact file:line and diff. Save to workspace as resume_shell_store.md.",
     ["resume-envvar"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-find-checkpoint",
     "Finds the latest checkpoint for the given run_id inside RecoveryShell resume flow.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE discovery call. Scope: RecoveryShell.run() pre-invoke only. Without finding the checkpoint, restore has nothing to restore from.",
     "Read graph_pipeline.py (RecoveryShell). In run(), before graph.invoke(): if resume flag is set, call list_checkpoints() to find all checkpoints for the run_id, pick the latest by timestamp. Provide exact file:line and diff. Save to workspace as resume_find_checkpoint.md.",
     ["resume-shell-store", "list-checkpoints-filter"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-restore-timeline",
     "Copies the selected checkpoint to timeline.otio inside RecoveryShell resume flow.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE copy operation. Scope: RecoveryShell.run() pre-invoke only. Without copying the checkpoint to the working timeline path, the graph has no timeline to operate on.",
     "Read graph_pipeline.py (RecoveryShell). In run(), after finding the checkpoint: copy the selected checkpoint file to {timeline_dir}/timeline.otio using shutil.copy2() or equivalent. Provide exact file:line and diff. Save to workspace as resume_restore_timeline.md.",
     ["resume-find-checkpoint"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-discover-completed",
     "Discovers which stages completed before the crash by reading checkpoint metadata.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE discovery loop. Scope: RecoveryShell.run() pre-invoke only. Without discovering completed stages, the graph re-runs everything from scene 1.",
     "Read graph_pipeline.py (RecoveryShell). In run(), before graph.invoke(): call list_checkpoints(), read each .meta.json, collect stages where completed=True. Provide exact file:line and diff. Save to workspace as resume_discover_completed.md.",
     ["resume-restore-timeline"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-inject-skip",
     "Injects the list of completed stages into the graph state so they are skipped.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE state injection. Scope: RecoveryShell.run() pre-invoke only. Without injecting skip state, the graph has no knowledge of which stages to bypass.",
     "Read graph_pipeline.py (RecoveryShell). In run(), before graph.invoke(): inject state['_skip_stages'] = [list of completed stage names] into the graph invocation state. Provide exact file:line and diff. Save to workspace as resume_inject_skip.md.",
     ["resume-discover-completed"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-scenario-skip-edge",
     "Adds the edge condition that skips the Scenario stage if already completed.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE edge condition. Scope: graph_pipeline.py Scenario stage entry only. Without this, a resumed run re-plans all 20 scenes even when scenario was already done.",
     "Read graph_pipeline.py. Add an edge condition for the Scenario stage entry that returns True (skip) if 'scenario' is in state['_skip_stages']. Provide exact file:line and diff. Save to workspace as resume_scenario_skip_edge.md.",
     ["resume-inject-skip"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-audio-skip-edge",
     "Adds the edge condition that skips the Audio stage if already completed.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE edge condition. Scope: graph_pipeline.py Audio stage entry only. Without this, a resumed run re-generates all narration even when audio was already done.",
     "Read graph_pipeline.py. Add an edge condition for the Audio stage entry that returns True (skip) if 'audio' is in state['_skip_stages']. Provide exact file:line and diff. Save to workspace as resume_audio_skip_edge.md.",
     ["resume-inject-skip"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-video-skip-edge",
     "Adds the edge condition that skips the Video stage if already completed.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE edge condition. Scope: graph_pipeline.py Video stage entry only. Without this, a resumed run re-renders all clips even when video was already done — the most expensive waste.",
     "Read graph_pipeline.py. Add an edge condition for the Video stage entry that returns True (skip) if 'video' is in state['_skip_stages']. Provide exact file:line and diff. Save to workspace as resume_video_skip_edge.md.",
     ["resume-inject-skip"],
     "server/strands_agents/graph_pipeline.py"),

    ("resume-assembly-skip-edge",
     "Adds the edge condition that skips the Assembly stage if already completed.",
     "resume", "pass2_resume.md",
     "You are a systems engineer adding ONE edge condition. Scope: graph_pipeline.py Assembly stage entry only. Without this, a resumed run re-assembles the final movie even when assembly was already done.",
     "Read graph_pipeline.py. Add an edge condition for the Assembly stage entry that returns True (skip) if 'assembly' is in state['_skip_stages']. Provide exact file:line and diff. Save to workspace as resume_assembly_skip_edge.md.",
     ["resume-inject-skip"],
     "server/strands_agents/graph_pipeline.py"),

    # ═══════════════════════════════════════════════════════════════════════════
    # DOMAIN 3 — Tolerance (NO SILENT FAILURES)
    # ═══════════════════════════════════════════════════════════════════════════
    ("recovery-budget-checkpoint",
     "Saves a checkpoint BEFORE escalating when a scene exhausts its retry budget.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: RecoveryManager.handle_failure() budget exhaustion branch only. When a scene exhausts its budget, the pipeline currently escalates and loses ALL work since the last checkpoint. It must checkpoint FIRST, then escalate with full context. NO SILENT FAILURES — the human must know exactly what stopped and why.",
     "Read recovery/manager.py lines ~73-80. BEFORE returning ESCALATE, add a call to save a checkpoint of the current timeline state. Log a CRITICAL message with scene number, failure class, attempts made, and budget limit. Then return ESCALATE. The human can inspect the checkpoint and decide how to proceed. NO SKIP_SCENE — failures must be visible. Provide exact before/after diff. Save to workspace as recovery_budget_checkpoint.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/recovery/manager.py"),

    ("recovery-repeat-checkpoint",
     "Saves a checkpoint BEFORE escalating when a scene fails repeatedly.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: RecoveryManager.handle_failure() repeated failures branch only. When a scene fails 3+ times, the pipeline currently escalates without saving state. It must checkpoint FIRST. NO SILENT FAILURES.",
     "Read recovery/manager.py lines ~89-96. BEFORE returning ESCALATE, add a call to save a checkpoint. Log a CRITICAL message with scene number, failure class, and failure history. Then return ESCALATE. NO SKIP_SCENE. Provide exact before/after diff. Save to workspace as recovery_repeat_checkpoint.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/recovery/manager.py"),

    ("recovery-global-checkpoint",
     "Saves a checkpoint BEFORE escalating when the global attempt cap is hit.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE checkpoint call. Scope: RecoveryManager.handle_failure() global cap branch only. When total attempts hit 50, the pipeline currently escalates without preserving state. It must checkpoint FIRST. NO SILENT FAILURES.",
     "Read recovery/manager.py lines ~98-109. BEFORE returning ESCALATE, add a call to save a checkpoint. Log a CRITICAL message with total_run_attempts, scene number, and global cap. Then return ESCALATE. NO SKIP_SCENE. Provide exact before/after diff. Save to workspace as recovery_global_checkpoint.md.",
     ["checkpoint-meta-sidecar"],
     "server/strands_agents/recovery/manager.py"),

    ("recovery-error-context",
     "Enriches escalation error messages with full context for human diagnosis.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer improving ONE error message builder. Scope: RecoveryManager._build_escalation_reason() only. When the pipeline stops, the human needs to know exactly what failed, how many times, and what to do next. NO SILENT FAILURES.",
     "Read recovery/manager.py _build_escalation_reason(). Enhance it to include: scene number, failure class, number of attempts, budget used, last error message snippet, and a suggested human action (e.g., 'check GPU quota', 'review prompt', 'manually generate scene_N.mp4'). The message must be suitable for a CRITICAL log entry and for an alert file. Provide exact before/after diff. Save to workspace as recovery_error_context.md.",
     [],
     "server/strands_agents/recovery/manager.py"),

    ("recovery-human-alert-file",
     "Writes a machine-readable alert file when the pipeline stops for human intervention.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE file write. Scope: RecoveryManager.handle_failure() escalation paths only. When the pipeline stops, it must leave a persistent alert that a human can discover without reading logs. NO SILENT FAILURES.",
     "Read recovery/manager.py. In every ESCALATE return path, BEFORE returning: write a JSON file to /tmp/documentary_output/ALERT_SCENE_{scene_num}.json containing: scene_num, failure_class, attempts, reason, timestamp, checkpoint_path, suggested_action. This file serves as a persistent signal that human intervention is needed. Provide exact file:line and diff. Save to workspace as recovery_human_alert_file.md.",
     ["recovery-error-context"],
     "server/strands_agents/recovery/manager.py"),

    ("recovery-escalate-log-level",
     "Ensures all escalation paths log at CRITICAL level, not WARNING.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer changing log levels. Scope: RecoveryManager.handle_failure() all escalation branches. Currently, some paths may log at WARNING. A pipeline-stop event must be CRITICAL so monitoring systems alert immediately. NO SILENT FAILURES.",
     "Read recovery/manager.py all ESCALATE paths. Ensure every log message before ESCALATE uses logger.critical() (not .warning() or .error()). Include scene number, failure details, and the fact that the pipeline is stopping. Provide exact before/after diff. Save to workspace as recovery_escalate_log_level.md.",
     ["recovery-human-alert-file"],
     "server/strands_agents/recovery/manager.py"),

    ("audio-exists-check",
     "Adds the file-existence check at the start of audio scene generation.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE existence check. Scope: audio_stage.py only. This is the gate that decides whether TTS runs or the existing file is reused.",
     "Read audio_stage.py. At the start of generate_scene_narration(): compute expected WAV path as os.path.join('/tmp/documentary_output', 'audio', f'scene_{scene_num:03d}_{voice}.wav'). Check os.path.exists(). If true, skip to reuse logic. Provide exact file:line and diff. Save to workspace as audio_exists_check.md.",
     [],
     "server/strands_agents/stages/audio_stage.py"),

    ("audio-ffprobe-duration",
     "Extracts audio duration via ffprobe when reusing an existing audio file.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE subprocess call. Scope: audio_stage.py reuse path only. The returned JSON must include the accurate duration of the reused file.",
     "Read audio_stage.py. In the reuse path: run ffprobe -v quiet -show_entries format=duration -of csv=p=0 on the existing WAV file. Parse the float duration. Default to 8.0 if ffprobe fails. Provide exact file:line and diff. Save to workspace as audio_ffprobe_duration.md.",
     ["audio-exists-check"],
     "server/strands_agents/stages/audio_stage.py"),

    ("audio-reuse-return",
     "Constructs the reuse JSON return value for audio scene generation.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE return statement. Scope: audio_stage.py reuse path only. The JSON tells the pipeline the scene is already done.",
     "Read audio_stage.py. In the reuse path: return json.dumps({'wav_path': path, 'duration': duration, 'scene_num': scene_num, 'voice': voice, 'note': 'reused existing file'}). Provide exact file:line and diff. Save to workspace as audio_reuse_return.md.",
     ["audio-ffprobe-duration"],
     "server/strands_agents/stages/audio_stage.py"),

    ("production-exists-check",
     "Adds the file-existence check at the start of video scene generation.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE existence check. Scope: production_stage.py only. This is the gate that decides whether video generation runs or the existing file is reused.",
     "Read production_stage.py. At the start of launch_visual_production(): compute expected MP4 path. Check os.path.exists(). If true, skip to reuse logic. Provide exact file:line and diff. Save to workspace as production_exists_check.md.",
     [],
     "server/strands_agents/stages/production_stage.py"),

    ("production-ffprobe-duration",
     "Extracts video duration via ffprobe when reusing an existing video file.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE subprocess call. Scope: production_stage.py reuse path only. The returned JSON must include the accurate duration of the reused file.",
     "Read production_stage.py. In the reuse path: run ffprobe -v quiet -show_entries format=duration -of csv=p=0 on the existing MP4 file. Parse the float duration. Default to 5.0 if ffprobe fails. Provide exact file:line and diff. Save to workspace as production_ffprobe_duration.md.",
     ["production-exists-check"],
     "server/strands_agents/stages/production_stage.py"),

    ("production-reuse-return",
     "Constructs the reuse JSON return value for video scene generation.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE return statement. Scope: production_stage.py reuse path only. The JSON tells the pipeline the scene is already done.",
     "Read production_stage.py. In the reuse path: return json.dumps({'mp4_path': path, 'duration': duration, 'scene_num': scene_num, 'note': 'reused existing file'}). Provide exact file:line and diff. Save to workspace as production_reuse_return.md.",
     ["production-ffprobe-duration"],
     "server/strands_agents/stages/production_stage.py"),

    ("assembly-detect-missing-video",
     "Changes assembly from hard-failing to detecting missing video clips with CRITICAL logging.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer changing ONE error path for video clips. Scope: assembly_tools.py video clip detection only. Currently, one missing video clip kills the entire assembly with a hard error. It must detect the missing clip, log a CRITICAL error with scene number and path, record the gap for the final report, and signal that a placeholder is needed. NO SILENT FAILURES.",
     "Read assembly_tools.py. In concat_clips() or assemble_documentary(), find where video clip missing causes hard error. Change to: detect missing video clip, log CRITICAL with scene number + path + expected duration, append to a gaps list, and continue. The CRITICAL log ensures the gap is visible. Provide exact file:line and diff. Save to workspace as assembly_detect_missing_video.md.",
     [],
     "server/tools/assembly_tools.py"),

    ("assembly-detect-missing-audio",
     "Changes assembly from hard-failing to detecting missing audio clips with CRITICAL logging.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer changing ONE error path for audio clips. Scope: assembly_tools.py audio clip detection only. Currently, one missing audio clip kills the entire assembly with a hard error. It must detect the missing clip, log a CRITICAL error with scene number and path, record the gap for the final report, and signal that a placeholder is needed. NO SILENT FAILURES.",
     "Read assembly_tools.py. In concat_clips() or assemble_documentary(), find where audio clip missing causes hard error. Change to: detect missing audio clip, log CRITICAL with scene number + path + expected duration, append to a gaps list, and continue. The CRITICAL log ensures the gap is visible. Provide exact file:line and diff. Save to workspace as assembly_detect_missing_audio.md.",
     [],
     "server/tools/assembly_tools.py"),

    ("assembly-video-placeholder-ffmpeg",
     "Implements the ffmpeg command for black-frame video placeholder generation.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer implementing ONE ffmpeg command. Scope: assembly_tools.py video placeholder only. When a scene's video is missing, the movie needs a black frame to maintain timing. BUT this must NEVER be silent — the caller must log CRITICAL that a placeholder is being used.",
     "Read assembly_tools.py. Implement generate_video_placeholder(duration, output_path) using ffmpeg color=c=black filter via subprocess. The command must create a valid video file of exactly the specified duration. The function docstring must state: 'Generates a placeholder for a missing clip. The caller MUST log a CRITICAL error before calling this function.' Provide exact code, insertion point, and ffmpeg command. Save to workspace as assembly_video_placeholder_ffmpeg.md.",
     [],
     "server/tools/assembly_tools.py"),

    ("assembly-video-placeholder-duration",
     "Ensures video placeholder duration matches the expected scene length from OTIO.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer implementing ONE duration lookup. Scope: assembly_tools.py video placeholder duration only. If the placeholder is the wrong duration, the final movie's timing drifts — audio and video go out of sync.",
     "Read assembly_tools.py. Implement duration extraction from OTIO metadata for missing video clips: read the expected duration from the timeline track item, pass it to generate_video_placeholder(). Provide exact code and insertion point. Save to workspace as assembly_video_placeholder_duration.md.",
     ["assembly-video-placeholder-ffmpeg"],
     "server/tools/assembly_tools.py"),

    ("assembly-audio-placeholder-ffmpeg",
     "Implements the ffmpeg command for silence audio placeholder generation.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer implementing ONE ffmpeg command. Scope: assembly_tools.py audio placeholder only. When a scene's audio is missing, the movie needs silence to maintain timing. BUT this must NEVER be silent — the caller must log CRITICAL that a placeholder is being used.",
     "Read assembly_tools.py. Implement generate_audio_placeholder(duration, output_path) using ffmpeg anullsrc filter via subprocess. The command must create a valid audio file of exactly the specified duration. The function docstring must state: 'Generates a placeholder for a missing clip. The caller MUST log a CRITICAL error before calling this function.' Provide exact code, insertion point, and ffmpeg command. Save to workspace as assembly_audio_placeholder_ffmpeg.md.",
     [],
     "server/tools/assembly_tools.py"),

    ("assembly-audio-placeholder-duration",
     "Ensures audio placeholder duration matches the expected scene length from OTIO.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer implementing ONE duration lookup. Scope: assembly_tools.py audio placeholder duration only. If the silence placeholder is the wrong duration, the final movie's audio track drifts relative to video.",
     "Read assembly_tools.py. Implement duration extraction from OTIO metadata for missing audio clips: read the expected duration from the timeline track item, pass it to generate_audio_placeholder(). Provide exact code and insertion point. Save to workspace as assembly_audio_placeholder_duration.md.",
     ["assembly-audio-placeholder-ffmpeg"],
     "server/tools/assembly_tools.py"),

    ("assembly-placeholder-cache-lookup",
     "Adds placeholder existence check so the same missing clip never regenerates its placeholder.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE existence check. Scope: assembly_tools.py placeholder lookup only. Without caching, every assembly re-run regenerates all placeholders even for the same missing clips, wasting CPU.",
     "Read assembly_tools.py. Add a check before generating any placeholder: if the placeholder file already exists in placeholders/, reuse it instead of calling ffmpeg. Provide exact code and insertion point. Save to workspace as assembly_placeholder_cache_lookup.md.",
     [],
     "server/tools/assembly_tools.py"),

    ("assembly-placeholder-cache-store",
     "Records generated placeholder paths so future lookups can find them.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE storage operation. Scope: assembly_tools.py placeholder recording only. Without recording, the lookup cache has nothing to find.",
     "Read assembly_tools.py. After generating a placeholder, record its path in a cache dict or file so subsequent lookups find it. Provide exact code and insertion point. Save to workspace as assembly_placeholder_cache_store.md.",
     ["assembly-placeholder-cache-lookup"],
     "server/tools/assembly_tools.py"),

    ("assembly-video-placeholder-integrate",
     "Wires video placeholder fallback into assembly with mandatory CRITICAL logging.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer wiring ONE placeholder type into assembly. Scope: assembly_tools.py video clip loop only. When a video clip is missing, a placeholder is used so assembly continues. BUT the gap must be LOUDLY logged as CRITICAL — NO SILENT FAILURES.",
     "Read assembly_tools.py. In the video concat loop: when a video clip is missing, FIRST log CRITICAL with scene number and path. THEN call generate_video_placeholder(). Use the placeholder in the concat list. Provide exact file:line and diff. Save to workspace as assembly_video_placeholder_integrate.md.",
     ["assembly-detect-missing-video", "assembly-video-placeholder-duration", "assembly-placeholder-cache-store"],
     "server/tools/assembly_tools.py"),

    ("assembly-audio-placeholder-integrate",
     "Wires audio placeholder fallback into assembly with mandatory CRITICAL logging.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer wiring ONE placeholder type into assembly. Scope: assembly_tools.py audio clip loop only. When an audio clip is missing, a placeholder is used so assembly continues. BUT the gap must be LOUDLY logged as CRITICAL — NO SILENT FAILURES.",
     "Read assembly_tools.py. In the audio concat loop: when an audio clip is missing, FIRST log CRITICAL with scene number and path. THEN call generate_audio_placeholder(). Use the placeholder in the concat list. Provide exact file:line and diff. Save to workspace as assembly_audio_placeholder_integrate.md.",
     ["assembly-detect-missing-audio", "assembly-audio-placeholder-duration", "assembly-placeholder-cache-store"],
     "server/tools/assembly_tools.py"),

    ("assembly-failure-report",
     "Writes a scene_failure_report.json after assembly listing every gap in the movie.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE file write. Scope: assemble_documentary() final step only. After assembly completes, a JSON report must list every scene that used a placeholder, so the human knows the movie has gaps. NO SILENT FAILURES.",
     "Read assembly_tools.py. At the end of assemble_documentary() or the main assembly function: write /tmp/documentary_output/scene_failure_report.json containing a list of all gaps detected during assembly (scene_num, clip_type, expected_path, placeholder_path, reason). This report is the final guarantee that no gap is silent. Provide exact file:line and diff. Save to workspace as assembly_failure_report.md.",
     ["assembly-video-placeholder-integrate", "assembly-audio-placeholder-integrate"],
     "server/tools/assembly_tools.py"),

    ("assembly-final-audit",
     "Adds a final audit step that asserts no unlogged gaps exist in the assembled movie.",
     "tolerance", "pass3_tolerance.md",
     "You are a systems engineer adding ONE assertion. Scope: assemble_documentary() final step only. Before declaring the movie complete, verify that every placeholder used was logged as CRITICAL and appears in the failure report. If any gap was not logged, raise RuntimeError immediately. NO SILENT FAILURES.",
     "Read assembly_tools.py. At the end of assemble_documentary(): compare the list of placeholders used against the list of CRITICAL logs emitted and the failure report. If any placeholder lacks a corresponding CRITICAL log entry, raise RuntimeError('SILENT GAP DETECTED: scene N has a placeholder but was not logged'). This is the final safety net. Provide exact file:line and diff. Save to workspace as assembly_final_audit.md.",
     ["assembly-failure-report"],
     "server/tools/assembly_tools.py"),
]


def compile_definitions():
    """Compile raw worker tuples into agent definition dicts with formatted task prompts."""
    definitions = []
    for name, desc, domain, traversal, sys, task, depends_on, target_file in WORKERS:
        definitions.append({
            "agent_name": name,
            "description": desc,
            "system_prompt": sys,
            "task_prompt_template": (
                f"## Mission: {desc}\n\n"
                f"TARGET FILE: {target_file}\n\n"
                f"{{source_file_content}}\n\n"
                f"Read the deep traversal report for your domain:\n"
                f"{{deep_traversal_report}}\n\n"
                f"Also read the pipeline analysis:\n"
                f"{{pipeline_analysis}}\n\n"
                f"### What you must do\n"
                f"{task}\n\n"
                f"### CRITICAL INSTRUCTION\n"
                f"You are writing PRODUCTION CODE — not reports, not diffs, not analysis.\n"
                f"Return ONLY the COMPLETE modified source file content.\n"
                f"The orchestrator will write your output directly to {target_file}.\n"
                f"Do NOT wrap your output in markdown, do NOT use diff format.\n"
                f"Just return the complete Python file content.\n\n"
                f"### Constraints\n"
                f"- You are an ISOLATED atomic worker. Your change is required for the movie to complete.\n"
                f"- Minimal changes. Only touch the exact file and function specified.\n"
                f"- Every change must be justified by graph evidence or source analysis.\n"
                f"- Do not modify tests, documentation, or unrelated code.\n"
                f"- PRESERVE ALL EXISTING CODE. Deleting functions, imports, or logic you don't understand will BREAK the pipeline.\n"
                f"- Other workers depend on every function in this file. If you delete something, their code will fail.\n"
                f"- You have access to the full codebase context. What looks 'unrelated' to you may be critical for another stage.\n"
                f"- ONLY add or modify what is specified. Never delete existing functions, helpers, or imports.\n"
                f"- If a function exists but you don't know what it does, LEAVE IT ALONE.\n"
            ),
            "deep_traversal_file": traversal,
            "depends_on": depends_on,
            "target_file": target_file,
        })
    return definitions


if __name__ == "__main__":
    defs = compile_definitions()
    print(f"Total workers: {len(defs)}")
    for i, d in enumerate(defs):
        deps = ", ".join(d["depends_on"]) if d["depends_on"] else "none"
        print(f"  {i+1:2d}. {d['agent_name']}  (depends: {deps})")
