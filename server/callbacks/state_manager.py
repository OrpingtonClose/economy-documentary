"""
Pipeline state initialization.

Provides ``build_pipeline_state()`` which returns the initial session state
dict for the documentary pipeline. All agents read and write to these keys
via ADK's session state (blackboard pattern).
"""

from __future__ import annotations

import uuid


def build_pipeline_state() -> dict:
    """Return the initial pipeline session state."""
    return {
        "_pipeline_key": f"doc_{uuid.uuid4().hex[:8]}",
        "topic": "",
        "corpus_path": "",
        "language": "en",
        "scenes": "[]",
        "content_analysis": "(not yet analyzed)",
        "visual_concepts": "(not yet generated)",
        "coherence_evaluation": "(not yet evaluated)",
        "whisperx_alignment": "{}",
        "otio_mutations": "[]",
        "otio_violation": None,
        "pipeline_phase": "idle",
        "lora_selections": "{}",
        "quick_test": "",
        # Template variables for scenario_director instructions.
        # Defaults are for standard (non-quick-test) mode.
        # run_pipeline.py and _init_pipeline_state override these
        # when DOCUMENTARY_QUICK_TEST is set.
        "quick_test_rules": "",
        "max_scene_duration": "45",
        "max_words_per_scene": "112",
    }
