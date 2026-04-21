"""Generate Tier-2 corpus seeds for components 02-15.

One-shot helper invoked during PR-E.1.  Writes 26 realistic seed
artifacts under ``server/strands_agents/corpus/seeds/`` and rewrites
``default_manifest.json`` with the new entries merged alongside the
existing five seeds.

Run from repo root:

    python scripts/generate_corpus_seeds.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SEED_ROOT = REPO / "server" / "strands_agents" / "corpus" / "seeds"
MANIFEST = REPO / "server" / "strands_agents" / "corpus" / "default_manifest.json"

# -- New seeds (component -> role -> payload, notes) ------------------------

NEW_SEEDS: list[dict[str, Any]] = [
    # 02-timing-evaluator: golden + adversarial (already has ambiguous)
    {
        "key": "timing.golden.within_tolerance",
        "component": "02-timing-evaluator",
        "role": "golden",
        "content_type": "timing_report_json",
        "seed_path": "timing_report_golden_within_tolerance.json",
        "expected_verdict": "accept",
        "notes": (
            "Every per-scene measurement within 0.5s of target, aggregate "
            "362.0s vs target 360.0s (0.5% drift).  Timing evaluator MUST "
            "accept without triggering a refine loop."
        ),
        "payload": {
            "revision": "rev-002",
            "target_duration_seconds": 360,
            "measured_total_seconds": 362.0,
            "per_scene": [
                {"scene_id": "s01", "target_seconds": 14.0, "measured_seconds": 14.2, "gap_before_seconds": 0.3, "words": 42},
                {"scene_id": "s02", "target_seconds": 18.0, "measured_seconds": 18.1, "gap_before_seconds": 0.4, "words": 54},
                {"scene_id": "s03", "target_seconds": 16.0, "measured_seconds": 16.1, "gap_before_seconds": 0.3, "words": 48},
                {"scene_id": "s04", "target_seconds": 20.0, "measured_seconds": 20.3, "gap_before_seconds": 0.5, "words": 60},
                {"scene_id": "s05", "target_seconds": 15.0, "measured_seconds": 15.2, "gap_before_seconds": 0.3, "words": 45},
            ],
            "tolerance_seconds": 2.0,
            "notes": "Golden: all scenes within tolerance, aggregate 0.5% drift.",
        },
    },
    {
        "key": "timing.adversarial.severe_overflow",
        "component": "02-timing-evaluator",
        "role": "adversarial",
        "content_type": "timing_report_json",
        "seed_path": "timing_report_adversarial_severe_overflow.json",
        "expected_verdict": "reject",
        "notes": (
            "Aggregate 436s vs 360s target (21% overflow) with multiple "
            "scenes 40%+ over.  Timing evaluator MUST reject; accepting "
            "this is a silent pipeline failure."
        ),
        "payload": {
            "revision": "rev-002",
            "target_duration_seconds": 360,
            "measured_total_seconds": 436.5,
            "per_scene": [
                {"scene_id": "s01", "target_seconds": 14.0, "measured_seconds": 19.8, "gap_before_seconds": 0.5, "words": 68},
                {"scene_id": "s02", "target_seconds": 18.0, "measured_seconds": 26.2, "gap_before_seconds": 0.8, "words": 86},
                {"scene_id": "s03", "target_seconds": 16.0, "measured_seconds": 22.5, "gap_before_seconds": 0.5, "words": 72},
                {"scene_id": "s04", "target_seconds": 20.0, "measured_seconds": 28.0, "gap_before_seconds": 0.5, "words": 92},
                {"scene_id": "s05", "target_seconds": 15.0, "measured_seconds": 21.0, "gap_before_seconds": 0.5, "words": 66},
            ],
            "tolerance_seconds": 2.0,
            "notes": "Adversarial: 21% aggregate overflow, per-scene drifts 40%+.",
        },
    },
    # 03-scenario-refiner
    {
        "key": "refine.golden.grounded_trim",
        "component": "03-scenario-refiner",
        "role": "golden",
        "content_type": "refined_scenario_json",
        "seed_path": "refine_golden_grounded_trim.json",
        "expected_verdict": "accept",
        "notes": (
            "Refiner read the timing report citing s02 overflow and "
            "trimmed s02 narration from 86 words to 54, preserving "
            "topical content and pronunciation hints.  Change is "
            "directly traceable to the report."
        ),
        "payload": {
            "revision": "rev-003",
            "parent_revision": "rev-002",
            "timing_report_ref": "timing.adversarial.severe_overflow",
            "changes": [
                {
                    "scene_id": "s02",
                    "kind": "trim_narration",
                    "before_words": 86,
                    "after_words": 54,
                    "rationale": "s02 measured 26.2s vs 18.0s target (overflow 8.2s); trimmed 32 words to align with target at 150 wpm.",
                },
            ],
            "scenes": [
                {"scene_id": "s01", "duration_seconds": 14, "narration": "…", "words": 42},
                {"scene_id": "s02", "duration_seconds": 18, "narration": "…", "words": 54, "edited": True},
                {"scene_id": "s03", "duration_seconds": 16, "narration": "…", "words": 48},
            ],
            "pronunciation_hints_preserved": True,
            "style_lock_preserved": True,
            "notes": "Golden refinement: cites report, targeted edit, preserves invariants.",
        },
    },
    {
        "key": "refine.adversarial.ignores_report",
        "component": "03-scenario-refiner",
        "role": "adversarial",
        "content_type": "refined_scenario_json",
        "seed_path": "refine_adversarial_ignores_report.json",
        "expected_verdict": "reject",
        "notes": (
            "Refiner was given an overflow report citing s02 but instead "
            "added a new scene s06 and changed the topic to WWII.  No "
            "rationale tied to the timing report; pronunciation hints "
            "dropped; style lock violated."
        ),
        "payload": {
            "revision": "rev-003",
            "parent_revision": "rev-002",
            "timing_report_ref": "timing.adversarial.severe_overflow",
            "changes": [
                {
                    "scene_id": "s06",
                    "kind": "add_scene",
                    "rationale": "I thought the documentary needed more historical context.",
                },
            ],
            "scenes": [
                {"scene_id": "s01", "duration_seconds": 14, "narration": "…", "words": 42},
                {"scene_id": "s02", "duration_seconds": 18, "narration": "…", "words": 86},
                {"scene_id": "s03", "duration_seconds": 16, "narration": "…", "words": 48},
                {"scene_id": "s06", "duration_seconds": 30, "narration": "World War II changed everything …", "words": 95, "added": True},
            ],
            "pronunciation_hints_preserved": False,
            "style_lock_preserved": False,
            "notes": "Adversarial refinement: ignores report, drifts topic, drops invariants.",
        },
    },
    # 04-audio-agent
    {
        "key": "audio.golden.spec_compliant",
        "component": "04-audio-agent",
        "role": "golden",
        "content_type": "audio_qa_report_json",
        "seed_path": "audio_golden_spec_compliant.json",
        "expected_verdict": "accept",
        "notes": (
            "LUFS -23.1 (target -23), peak -3.2 dBFS (no clipping), "
            "alignment confidence 0.94, no gaps > 0.8s, single voice.  "
            "Audio agent MUST accept."
        ),
        "payload": {
            "scene_id": "s01",
            "voice_id": "narrator_primary",
            "duration_seconds": 14.2,
            "lufs": -23.1,
            "peak_dbfs": -3.2,
            "alignment_confidence": 0.94,
            "gaps": [{"start": 3.2, "duration": 0.3}, {"start": 9.4, "duration": 0.4}],
            "clipping_events": 0,
            "voice_count_detected": 1,
            "pronunciation_hints_honored": ["OPEC", "Keynesian"],
            "notes": "Golden: LUFS within 0.5LU of target, no clips, single voice.",
        },
    },
    {
        "key": "audio.adversarial.hot_and_clipping",
        "component": "04-audio-agent",
        "role": "adversarial",
        "content_type": "audio_qa_report_json",
        "seed_path": "audio_adversarial_hot_and_clipping.json",
        "expected_verdict": "reject",
        "notes": (
            "LUFS -14 (9 LU over target), 27 clipping events, alignment "
            "confidence 0.62, two distinct voices detected (contract "
            "violation — one voice per scene).  Audio agent MUST reject."
        ),
        "payload": {
            "scene_id": "s02",
            "voice_id": "narrator_primary",
            "duration_seconds": 18.0,
            "lufs": -14.2,
            "peak_dbfs": 0.1,
            "alignment_confidence": 0.62,
            "gaps": [{"start": 2.1, "duration": 2.8}],
            "clipping_events": 27,
            "voice_count_detected": 2,
            "pronunciation_hints_honored": [],
            "notes": "Adversarial: mastered too loud, clipping throughout, second voice bleed.",
        },
    },
    # 05-timing-loop
    {
        "key": "timing_loop.golden.converges",
        "component": "05-timing-loop",
        "role": "golden",
        "content_type": "loop_trace_json",
        "seed_path": "timing_loop_golden_converges.json",
        "expected_verdict": "accept",
        "notes": (
            "Loop runs 3 iterations, each refine cites the prior timing "
            "report and edits the specific scene(s) flagged.  Converges "
            "with aggregate drift 1.2s."
        ),
        "payload": {
            "iterations": [
                {"i": 1, "measured_total": 402.0, "refine_cited_report": True, "scenes_edited": ["s02", "s04"]},
                {"i": 2, "measured_total": 371.0, "refine_cited_report": True, "scenes_edited": ["s04"]},
                {"i": 3, "measured_total": 361.2, "verdict": "accept"},
            ],
            "final_verdict": "accept",
            "escalation_triggered": False,
            "notes": "Golden trajectory: monotonic convergence, grounded refinements.",
        },
    },
    {
        "key": "timing_loop.adversarial.thrash_ten_iters",
        "component": "05-timing-loop",
        "role": "adversarial",
        "content_type": "loop_trace_json",
        "seed_path": "timing_loop_adversarial_thrash_ten_iters.json",
        "expected_verdict": "reject",
        "notes": (
            "Loop runs the full 10 iteration budget with drift "
            "oscillating between 380s and 420s.  Refines don't cite the "
            "report; no escalation triggered at the cap.  The orchestrator "
            "failed to detect the no-op refiner pattern."
        ),
        "payload": {
            "iterations": [
                {"i": i, "measured_total": 400 + (-1) ** i * 20, "refine_cited_report": False, "scenes_edited": []}
                for i in range(1, 11)
            ],
            "final_verdict": "max_iterations_reached",
            "escalation_triggered": False,
            "notes": "Adversarial: refiner no-op, orchestrator didn't escalate.",
        },
    },
    # 06-content-analyst
    {
        "key": "analysis.golden.grounded_motifs",
        "component": "06-content-analyst",
        "role": "golden",
        "content_type": "content_analysis_json",
        "seed_path": "analysis_golden_grounded_motifs.json",
        "expected_verdict": "accept",
        "notes": (
            "Per-scene visual motifs grounded in specific narration "
            "phrases, historical accuracy preserved, style-lock honored."
        ),
        "payload": {
            "scenario_revision": "rev-001",
            "per_scene": [
                {
                    "scene_id": "s01",
                    "narration_anchors": ["OPEC's oil embargo", "gas lines stretch around the block"],
                    "motifs": ["1973 gas station queues", "embargo-era newspaper headlines"],
                    "style_tags": ["archival black-and-white", "grainy film stock"],
                },
                {
                    "scene_id": "s02",
                    "narration_anchors": ["Phillips curve was gospel", "entire profession had to admit"],
                    "motifs": ["economists at chalkboards", "Federal Reserve boardroom 1970s"],
                    "style_tags": ["archival color", "handheld"],
                },
                {
                    "scene_id": "s03",
                    "narration_anchors": ["Milton Friedman", "1967 address"],
                    "motifs": ["University of Chicago lecture hall", "wire-rimmed glasses close-up"],
                    "style_tags": ["archival academic", "warm tungsten"],
                },
            ],
            "notes": "Golden: motifs anchor to specific narration phrases.",
        },
    },
    {
        "key": "analysis.adversarial.generic_stock",
        "component": "06-content-analyst",
        "role": "adversarial",
        "content_type": "content_analysis_json",
        "seed_path": "analysis_adversarial_generic_stock.json",
        "expected_verdict": "reject",
        "notes": (
            "Motifs are generic stock-footage descriptors with no "
            "connection to the narration.  Style lock ignored; suggests "
            "'people in suits' for a 1973-era scene."
        ),
        "payload": {
            "scenario_revision": "rev-001",
            "per_scene": [
                {"scene_id": "s01", "narration_anchors": [], "motifs": ["people in suits", "stock market graph"], "style_tags": ["generic corporate"]},
                {"scene_id": "s02", "narration_anchors": [], "motifs": ["office building"], "style_tags": ["stock"]},
                {"scene_id": "s03", "narration_anchors": [], "motifs": ["man at podium"], "style_tags": []},
            ],
            "notes": "Adversarial: no narration grounding, style lock violated.",
        },
    },
    # 07-visual-concepter
    {
        "key": "visual.golden.style_locked",
        "component": "07-visual-concepter",
        "role": "golden",
        "content_type": "visual_concept_json",
        "seed_path": "visual_golden_style_locked.json",
        "expected_verdict": "accept",
        "notes": (
            "Per-scene concepts match narration, honor documentary-"
            "archival-1970s style lock, camera notes align with era."
        ),
        "payload": {
            "style_lock": "documentary-archival-1970s",
            "scenes": [
                {
                    "scene_id": "s01",
                    "concept": "Static wide shot of a Sunoco gas station, cars queued along the shoulder, 1973 American flag visible, overcast sky.",
                    "camera": "static tripod, 35mm grain simulation, desaturated palette",
                    "palette": "#1f2a2e, #7a6a52, #d2b48c",
                },
                {
                    "scene_id": "s02",
                    "concept": "Slow push-in on a Federal Reserve boardroom, economists mid-debate, papers spread on polished walnut.",
                    "camera": "slow dolly-in, handheld simulation, warm tungsten",
                    "palette": "#3a2a1e, #a88455, #e7d4a3",
                },
                {
                    "scene_id": "s03",
                    "concept": "Medium shot of Milton Friedman at a University of Chicago lectern, 1967, addressing an audience out of frame.",
                    "camera": "locked-off medium, period-accurate lighting",
                    "palette": "#2e2018, #8a6542, #cfae88",
                },
            ],
            "notes": "Golden: on-style, on-topic, era-appropriate cinematography.",
        },
    },
    {
        "key": "visual.adversarial.style_drift",
        "component": "07-visual-concepter",
        "role": "adversarial",
        "content_type": "visual_concept_json",
        "seed_path": "visual_adversarial_style_drift.json",
        "expected_verdict": "reject",
        "notes": (
            "Scene 2 concept is a cartoon sequence despite documentary-"
            "archival style lock.  Scene 3 uses iPhone footage aesthetic "
            "for a 1967 subject.  Concepter MUST reject these."
        ),
        "payload": {
            "style_lock": "documentary-archival-1970s",
            "scenes": [
                {"scene_id": "s01", "concept": "Gas station queue, archival look.", "camera": "tripod", "palette": "#1f2a2e"},
                {"scene_id": "s02", "concept": "Cartoon sequence of dollar bills with wings flying away.", "camera": "2D animated", "palette": "#ff00ff"},
                {"scene_id": "s03", "concept": "iPhone vertical video of a man talking on stage.", "camera": "smartphone handheld", "palette": "#ffffff"},
            ],
            "notes": "Adversarial: s02 breaks style (cartoon), s03 breaks era (smartphone).",
        },
    },
    # 08-coherence-evaluator: needs adversarial only
    {
        "key": "critique.adversarial.unfounded_pass",
        "component": "08-coherence-evaluator",
        "role": "adversarial",
        "content_type": "critique_json",
        "seed_path": "critique_adversarial_unfounded_pass.json",
        "expected_verdict": "reject",
        "notes": (
            "Critique declares every scene coherent without citing any "
            "scene-level evidence, misses the cartoon-style break on "
            "scene 2, and suggests no edits despite clear style drift."
        ),
        "payload": {
            "revision": "rev-001",
            "overall_verdict": "coherent",
            "per_scene": [
                {"scene_id": "s01", "rating": "pass", "evidence": None, "style_violation": None},
                {"scene_id": "s02", "rating": "pass", "evidence": None, "style_violation": None},
                {"scene_id": "s03", "rating": "pass", "evidence": None, "style_violation": None},
            ],
            "suggested_edits": [],
            "notes": "Adversarial: rubber-stamp critique. Ignores style drift on s02.",
        },
    },
    # 09-visual-loop
    {
        "key": "visual_loop.golden.converges",
        "component": "09-visual-loop",
        "role": "golden",
        "content_type": "loop_trace_json",
        "seed_path": "visual_loop_golden_converges.json",
        "expected_verdict": "accept",
        "notes": (
            "Loop runs 2 iterations.  Critique flags style drift on s02; "
            "refiner targets s02 only; second critique passes."
        ),
        "payload": {
            "iterations": [
                {"i": 1, "critique_verdict": "reject", "flagged_scenes": ["s02"], "refine_targets": ["s02"]},
                {"i": 2, "critique_verdict": "accept", "flagged_scenes": [], "refine_targets": []},
            ],
            "final_verdict": "accept",
            "escalation_triggered": False,
            "notes": "Golden: targeted refinement, converges in 2 iters.",
        },
    },
    {
        "key": "visual_loop.adversarial.no_response_to_critique",
        "component": "09-visual-loop",
        "role": "adversarial",
        "content_type": "loop_trace_json",
        "seed_path": "visual_loop_adversarial_no_response.json",
        "expected_verdict": "reject",
        "notes": (
            "Critique flags style drift on s02 every iteration but "
            "concept on s02 is identical across all 5 iterations.  "
            "Refiner is a no-op; orchestrator failed to escalate."
        ),
        "payload": {
            "iterations": [
                {"i": i, "critique_verdict": "reject", "flagged_scenes": ["s02"], "refine_targets": ["s02"], "s02_concept_hash": "abc123"}
                for i in range(1, 6)
            ],
            "final_verdict": "max_iterations_reached",
            "escalation_triggered": False,
            "notes": "Adversarial: refiner no-op on s02, no escalation.",
        },
    },
    # 10-production-supervisor
    {
        "key": "production.golden.valid_plan",
        "component": "10-production-supervisor",
        "role": "golden",
        "content_type": "production_plan_json",
        "seed_path": "production_golden_valid_plan.json",
        "expected_verdict": "accept",
        "notes": (
            "Worker assignments non-colliding, per-scene budgets within "
            "total budget, backoff strategy documented, health checks "
            "passing for all workers before dispatch."
        ),
        "payload": {
            "total_budget_usd": 12.5,
            "workers": [
                {"worker_id": "ltx-a", "assigned_scenes": ["s01", "s03"], "est_cost_usd": 4.2, "status": "ready"},
                {"worker_id": "ltx-b", "assigned_scenes": ["s02", "s04"], "est_cost_usd": 4.4, "status": "ready"},
                {"worker_id": "tts-a", "assigned_scenes": ["s01", "s02", "s03", "s04", "s05"], "est_cost_usd": 1.8, "status": "ready"},
            ],
            "total_est_cost_usd": 10.4,
            "backoff_strategy": "exponential, base=2s, max=60s, max_retries=3",
            "notes": "Golden: workers ready, within budget, backoff documented.",
        },
    },
    {
        "key": "production.adversarial.over_budget_collisions",
        "component": "10-production-supervisor",
        "role": "adversarial",
        "content_type": "production_plan_json",
        "seed_path": "production_adversarial_over_budget.json",
        "expected_verdict": "reject",
        "notes": (
            "Estimated cost $18 vs $12.5 budget, two workers assigned to "
            "s02 (collision), no backoff plan, one worker marked "
            "'unhealthy' yet still dispatched.  Supervisor MUST reject."
        ),
        "payload": {
            "total_budget_usd": 12.5,
            "workers": [
                {"worker_id": "ltx-a", "assigned_scenes": ["s01", "s02"], "est_cost_usd": 6.5, "status": "ready"},
                {"worker_id": "ltx-b", "assigned_scenes": ["s02", "s03"], "est_cost_usd": 6.8, "status": "unhealthy"},
                {"worker_id": "tts-a", "assigned_scenes": ["s01", "s02", "s03"], "est_cost_usd": 4.8, "status": "ready"},
            ],
            "total_est_cost_usd": 18.1,
            "backoff_strategy": None,
            "notes": "Adversarial: over budget, collision on s02, unhealthy worker dispatched.",
        },
    },
    # 11-assembly-agent
    {
        "key": "assembly.golden.valid_otio",
        "component": "11-assembly-agent",
        "role": "golden",
        "content_type": "otio_summary_json",
        "seed_path": "assembly_golden_valid_otio.json",
        "expected_verdict": "accept",
        "notes": (
            "OTIO timeline with no gaps, matching audio and video track "
            "durations, markers at every scene boundary, no overlaps."
        ),
        "payload": {
            "timeline_duration_seconds": 360.0,
            "tracks": [
                {"kind": "video", "clip_count": 5, "total_duration": 360.0, "gaps": []},
                {"kind": "audio_narration", "clip_count": 5, "total_duration": 360.0, "gaps": []},
                {"kind": "audio_score", "clip_count": 1, "total_duration": 360.0, "gaps": []},
            ],
            "markers": [
                {"scene_id": "s01", "time": 0.0, "kind": "scene_boundary"},
                {"scene_id": "s02", "time": 14.0, "kind": "scene_boundary"},
                {"scene_id": "s03", "time": 32.0, "kind": "scene_boundary"},
                {"scene_id": "s04", "time": 48.0, "kind": "scene_boundary"},
                {"scene_id": "s05", "time": 68.0, "kind": "scene_boundary"},
            ],
            "overlap_count": 0,
            "gap_count": 0,
            "notes": "Golden: clean OTIO, zero gaps, zero overlaps.",
        },
    },
    {
        "key": "assembly.adversarial.gaps_and_overlaps",
        "component": "11-assembly-agent",
        "role": "adversarial",
        "content_type": "otio_summary_json",
        "seed_path": "assembly_adversarial_gaps_and_overlaps.json",
        "expected_verdict": "reject",
        "notes": (
            "3 gaps totaling 4.2s on video track, 2 overlaps between "
            "audio_narration clips, missing score on scenes 3-4.  "
            "Assembly MUST reject before export."
        ),
        "payload": {
            "timeline_duration_seconds": 364.2,
            "tracks": [
                {"kind": "video", "clip_count": 5, "total_duration": 360.0, "gaps": [{"start": 32.0, "duration": 1.5}, {"start": 68.0, "duration": 1.2}, {"start": 320.0, "duration": 1.5}]},
                {"kind": "audio_narration", "clip_count": 5, "total_duration": 362.3, "overlaps": [{"scene_a": "s02", "scene_b": "s03", "duration": 1.3}, {"scene_a": "s04", "scene_b": "s05", "duration": 1.0}]},
                {"kind": "audio_score", "clip_count": 1, "total_duration": 180.0, "covers_scenes": ["s01", "s02", "s05"]},
            ],
            "markers": [],
            "overlap_count": 2,
            "gap_count": 3,
            "notes": "Adversarial: gaps, overlaps, missing score coverage.",
        },
    },
    # 12-recovery-agents
    {
        "key": "recovery.golden.retry_transient",
        "component": "12-recovery-agents",
        "role": "golden",
        "content_type": "recovery_decision_json",
        "seed_path": "recovery_golden_retry_transient.json",
        "expected_verdict": "accept",
        "notes": (
            "Transient HTTP 503 from LTX worker, retry_count=0, budget "
            "healthy.  Correct decision: retry with 2s backoff.  Fix or "
            "escalate would be wasteful."
        ),
        "payload": {
            "failure": {
                "kind": "transient",
                "http_status": 503,
                "worker_id": "ltx-a",
                "scene_id": "s02",
                "error": "upstream connection timeout",
            },
            "context": {
                "retry_count": 0,
                "fix_count": 0,
                "budget_remaining_usd": 8.2,
                "total_budget_usd": 12.5,
            },
            "decision": "retry",
            "backoff_seconds": 2.0,
            "next_revision_suffix": 1,
            "rationale": "Transient 503 with 0 retries and healthy budget; exponential backoff appropriate.",
        },
    },
    {
        "key": "recovery.adversarial.retry_deterministic",
        "component": "12-recovery-agents",
        "role": "adversarial",
        "content_type": "recovery_decision_json",
        "seed_path": "recovery_adversarial_retry_deterministic.json",
        "expected_verdict": "reject",
        "notes": (
            "Deterministic corruption (scene payload schema-invalid).  "
            "Retrying the same input will keep failing.  Correct call "
            "is fix (schema repair) or escalate.  Agent proposed "
            "retry; recovery supervisor MUST reject."
        ),
        "payload": {
            "failure": {
                "kind": "deterministic",
                "error_class": "SchemaValidationError",
                "worker_id": "ltx-b",
                "scene_id": "s04",
                "error": "scene payload missing required field 'visual_prompt'",
            },
            "context": {
                "retry_count": 2,
                "fix_count": 0,
                "budget_remaining_usd": 6.1,
                "total_budget_usd": 12.5,
            },
            "decision": "retry",
            "backoff_seconds": 8.0,
            "next_revision_suffix": 3,
            "rationale": "Going to try again, maybe it works this time.",
        },
    },
    # 13-escalation-supervisor: needs golden only
    {
        "key": "escalation.golden.exhausted_recovery",
        "component": "13-escalation-supervisor",
        "role": "golden",
        "content_type": "escalation_decision_json",
        "seed_path": "escalation_golden_exhausted_recovery.json",
        "expected_verdict": "accept",
        "notes": (
            "3 retries + 2 fixes exhausted on s02, budget 93% consumed, "
            "same SchemaValidationError persisting.  Supervisor MUST "
            "accept escalation; continuing would burn the rest of the "
            "budget."
        ),
        "payload": {
            "scene_id": "s02",
            "context": {
                "retry_count": 3,
                "fix_count": 2,
                "budget_remaining_usd": 0.9,
                "total_budget_usd": 12.5,
                "recent_failures": [
                    {"kind": "deterministic", "error_class": "SchemaValidationError"},
                    {"kind": "deterministic", "error_class": "SchemaValidationError"},
                    {"kind": "deterministic", "error_class": "SchemaValidationError"},
                ],
            },
            "decision": "escalate",
            "target": "human_operator",
            "rationale": "Recovery ladder exhausted with persistent deterministic error and 93% budget consumed.",
            "severity": "high",
        },
    },
    # 14-pipeline-graph
    {
        "key": "pipeline.golden.full_trajectory",
        "component": "14-pipeline-graph",
        "role": "golden",
        "content_type": "pipeline_trace_json",
        "seed_path": "pipeline_golden_full_trajectory.json",
        "expected_verdict": "accept",
        "notes": (
            "Complete trajectory: scenario -> timing loop (3 iters) -> "
            "content analysis -> visual concepter -> visual loop (2 "
            "iters) -> production -> assembly.  All approval gates "
            "respected; no stages skipped."
        ),
        "payload": {
            "stages": [
                {"stage": "scenario", "verdict": "accept", "revision": "rev-001"},
                {"stage": "timing_loop", "iterations": 3, "verdict": "accept", "revision": "rev-003"},
                {"stage": "content_analysis", "verdict": "accept"},
                {"stage": "visual_concepter", "verdict": "accept"},
                {"stage": "visual_loop", "iterations": 2, "verdict": "accept"},
                {"stage": "production", "verdict": "accept"},
                {"stage": "assembly", "verdict": "accept"},
            ],
            "approvals": [
                {"gate": "scenario_approval", "decision": "approve"},
                {"gate": "production_approval", "decision": "approve"},
                {"gate": "assembly_approval", "decision": "approve"},
            ],
            "stages_skipped": [],
            "notes": "Golden: all stages in order, approvals honored.",
        },
    },
    {
        "key": "pipeline.adversarial.skipped_critique",
        "component": "14-pipeline-graph",
        "role": "adversarial",
        "content_type": "pipeline_trace_json",
        "seed_path": "pipeline_adversarial_skipped_critique.json",
        "expected_verdict": "reject",
        "notes": (
            "Pipeline jumped from visual concepter directly to "
            "production, skipping the coherence evaluator + visual "
            "loop.  Style drift went uncaught; orchestrator MUST "
            "reject this trajectory."
        ),
        "payload": {
            "stages": [
                {"stage": "scenario", "verdict": "accept"},
                {"stage": "timing_loop", "iterations": 2, "verdict": "accept"},
                {"stage": "content_analysis", "verdict": "accept"},
                {"stage": "visual_concepter", "verdict": "accept"},
                {"stage": "production", "verdict": "accept"},
                {"stage": "assembly", "verdict": "accept"},
            ],
            "approvals": [
                {"gate": "scenario_approval", "decision": "approve"},
                {"gate": "production_approval", "decision": "approve"},
            ],
            "stages_skipped": ["coherence_evaluator", "visual_loop"],
            "notes": "Adversarial: critique + visual loop skipped.",
        },
    },
    # 15-approval-gates
    {
        "key": "approval.golden.scoped_request",
        "component": "15-approval-gates",
        "role": "golden",
        "content_type": "approval_request_json",
        "seed_path": "approval_golden_scoped_request.json",
        "expected_verdict": "accept",
        "notes": (
            "Approval request for launch_visual_production with full "
            "payload, blast-radius=high (5 GPU dispatches), cost "
            "estimate, and linked production plan.  User will be able "
            "to judge; approval gate MUST accept this shape."
        ),
        "payload": {
            "tool_name": "launch_visual_production",
            "gate_id": "production_approval",
            "payload": {
                "scene_ids": ["s01", "s02", "s03", "s04", "s05"],
                "production_plan_ref": "production.golden.valid_plan",
                "est_cost_usd": 10.4,
            },
            "blast_radius": "high",
            "allowed_decisions": ["approve", "reject", "edit"],
            "side_effects": ["spawn 5 GPU jobs", "burn ~$10.40 budget", "write OTIO artifact"],
            "notes": "Golden: scoped, blast-radius declared, side effects listed.",
        },
    },
    {
        "key": "approval.adversarial.auto_approve_destructive",
        "component": "15-approval-gates",
        "role": "adversarial",
        "content_type": "approval_request_json",
        "seed_path": "approval_adversarial_auto_approve_destructive.json",
        "expected_verdict": "reject",
        "notes": (
            "Request auto-approves a delete_b2_bucket call without "
            "human prompt, marks blast_radius='low', omits side "
            "effects.  Approval gate MUST reject this shape — the "
            "agent is trying to bypass HITL."
        ),
        "payload": {
            "tool_name": "delete_b2_bucket",
            "gate_id": "cleanup_approval",
            "payload": {"bucket": "cloudberry-documentary-v2"},
            "blast_radius": "low",
            "allowed_decisions": ["approve"],
            "auto_approved": True,
            "side_effects": [],
            "notes": "Adversarial: destructive op mislabeled, auto-approved.",
        },
    },
]


def _write_seed(entry: dict[str, Any]) -> tuple[int, str]:
    path = SEED_ROOT / entry["seed_path"]
    payload = json.dumps(entry["payload"], indent=2, ensure_ascii=False) + "\n"
    raw = payload.encode("utf-8")
    path.write_bytes(raw)
    return len(raw), hashlib.sha256(raw).hexdigest()


def main() -> None:
    existing = json.loads(MANIFEST.read_text())
    existing_keys = {a["key"] for a in existing["artifacts"]}

    added = 0
    for entry in NEW_SEEDS:
        if entry["key"] in existing_keys:
            continue
        size, sha = _write_seed(entry)
        existing["artifacts"].append(
            {
                "key": entry["key"],
                "component": entry["component"],
                "role": entry["role"],
                "content_type": entry["content_type"],
                "storage": "seed",
                "seed_path": entry["seed_path"],
                "sha256": sha,
                "size_bytes": size,
                "expected_verdict": entry["expected_verdict"],
                "notes": entry["notes"],
                "license": "CC-BY-4.0",
                "source": "hand-authored for strands corpus v1",
            }
        )
        added += 1

    existing["artifacts"].sort(key=lambda a: a["key"])
    MANIFEST.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {added} new seeds, manifest now has {len(existing['artifacts'])} artifacts")


if __name__ == "__main__":
    main()
