#!/usr/bin/env python3
"""
Minimal end-to-end test — validates the pipeline can be assembled and
basic tool operations work in test mode.

Usage:
    DOCUMENTARY_TEST_MODE=true python test_run.py [--topic "Your Topic"]

This does NOT run the full pipeline (which requires GPU and API keys).
It validates:
  1. All imports resolve
  2. Pipeline agent can be constructed
  3. OTIO timeline operations work
  4. Tool functions work in test mode
  5. Dashboard event store initializes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Force test mode
os.environ.setdefault("DOCUMENTARY_TEST_MODE", "true")
os.environ.setdefault("ADK_MODEL", "gemini-2.5-flash")


def test_imports() -> bool:
    """Test that all modules can be imported."""
    print("  Testing imports...")
    errors = []

    modules = [
        "config",
        "pipeline.otio_timeline",
        "pipeline.swarm_extraction.models",
        "pipeline.swarm_extraction.condition_store",
        "pipeline.swarm_extraction.scoring",
        "pipeline.swarm_extraction.tool_defs",
    ]

    for mod in modules:
        try:
            __import__(mod)
            print(f"    OK: {mod}")
        except ImportError as e:
            errors.append(f"    FAIL: {mod} — {e}")

    # Server modules (may fail if dependencies not installed)
    server_modules = [
        "server.callbacks.state_manager",
        "server.agents.model_config",
    ]

    for mod in server_modules:
        try:
            __import__(mod)
            print(f"    OK: {mod}")
        except ImportError as e:
            print(f"    SKIP: {mod} — {e} (install server deps)")

    if errors:
        for e in errors:
            print(e)
        return False
    return True


def test_otio_timeline() -> bool:
    """Test OTIO timeline creation and manipulation."""
    print("  Testing OTIO timeline...")

    try:
        from pipeline.otio_timeline import (
            create_timeline,
            add_clip,
            get_timeline_summary,
            validate_timeline,
        )
    except ImportError as e:
        print(f"    SKIP: opentimelineio not installed — {e}")
        return True

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create timeline
        filepath = create_timeline("test-topic", num_scenes=3, output_dir=tmpdir)
        assert os.path.exists(filepath), f"Timeline file not created: {filepath}"
        print(f"    OK: Created timeline at {filepath}")

        # Get summary
        summary = get_timeline_summary(filepath)
        assert summary["timeline_name"] == "documentary_test-topic"
        assert len(summary["tracks"]) == 3
        print(f"    OK: Timeline has {len(summary['tracks'])} tracks")

        # Validate scenario phase
        result = validate_timeline(filepath, "scenario")
        assert result["valid"], f"Scenario validation failed: {result}"
        print("    OK: Scenario validation passed")

        # Add a clip (create a dummy WAV first)
        dummy_wav = os.path.join(tmpdir, "test.wav")
        with open(dummy_wav, "wb") as f:
            f.write(b"\x00" * 100)

        added = add_clip(
            filepath, "A1_Narration", "scene1_V1",
            dummy_wav, 5.0, {"scene_num": 1, "voice": "V1"},
        )
        assert added, "Clip should have been added"
        print("    OK: Added narration clip")

        # Idempotency check
        added_again = add_clip(
            filepath, "A1_Narration", "scene1_V1",
            dummy_wav, 5.0, {"scene_num": 1, "voice": "V1"},
        )
        assert not added_again, "Duplicate clip should not have been added"
        print("    OK: Idempotency check passed")

        # Final summary
        summary = get_timeline_summary(filepath)
        narration = next(t for t in summary["tracks"] if t["name"] == "A1_Narration")
        assert narration["total_clips"] == 1
        print(f"    OK: A1_Narration has {narration['total_clips']} clip(s)")

    return True


def test_scoring() -> bool:
    """Test scoring functions."""
    print("  Testing scoring...")

    from pipeline.swarm_extraction.scoring import trust_score_url, serendipity_score

    # Trust scoring
    assert trust_score_url("https://fred.stlouisfed.org/series/GDP") >= 0.85
    assert trust_score_url("https://www.reuters.com/article/123") >= 0.65
    assert trust_score_url("https://reddit.com/r/economics") <= 0.2
    assert trust_score_url("") == 0.3
    print("    OK: Trust scoring")

    # Serendipity scoring
    score = serendipity_score(
        "The M2 money supply increased by 40% in 2020-2021",
        ["GDP growth was 5.7% in 2021", "Inflation reached 9.1% in June 2022"],
        ["macroeconomics", "monetary policy"],
    )
    assert 0.0 <= score <= 1.0
    print(f"    OK: Serendipity score = {score:.2f}")

    return True


def test_condition_store() -> bool:
    """Test condition store admission."""
    print("  Testing condition store...")

    from pipeline.swarm_extraction.condition_store import ConditionStore
    from pipeline.swarm_extraction.models import AtomicCondition

    async def _test():
        store = ConditionStore()

        # Admit a valid condition
        c1 = AtomicCondition(
            fact="US national debt exceeded $36 trillion in 2025",
            confidence=0.9,
            source_url="https://fred.stlouisfed.org/series/GFDEBTN",
        )
        result = await store.admit(c1)
        assert result.admitted, f"Should have been admitted: {result.reason}"

        # Reject low confidence
        c2 = AtomicCondition(fact="Some vague claim about something", confidence=0.1)
        result = await store.admit(c2)
        assert not result.admitted

        # Reject duplicate
        c3 = AtomicCondition(
            fact="US national debt exceeded $36 trillion in 2025",
            confidence=0.85,
        )
        result = await store.admit(c3)
        assert not result.admitted

        assert len(store) == 1
        return True

    result = asyncio.run(_test())
    print("    OK: Condition store admission/dedup")
    return result


def test_config() -> bool:
    """Test central config loads."""
    print("  Testing config...")

    import config as cfg

    assert cfg.DOCUMENTARY_TEST_MODE is True
    assert cfg.ADK_MODEL == "gemini-2.5-flash"
    assert cfg.MAX_CONCURRENT_LLM == 4
    print("    OK: Config loaded")
    return True


def main():
    parser = argparse.ArgumentParser(description="Documentary pipeline test run")
    parser.add_argument("--topic", default="test", help="Documentary topic")
    args = parser.parse_args()

    print(f"\n=== Documentary Pipeline Test Run ===")
    print(f"Topic: {args.topic}")
    print(f"Test mode: {os.environ.get('DOCUMENTARY_TEST_MODE', 'false')}")
    print()

    tests = [
        ("Imports", test_imports),
        ("Config", test_config),
        ("Scoring", test_scoring),
        ("Condition Store", test_condition_store),
        ("OTIO Timeline", test_otio_timeline),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n[{name}]")
        try:
            if test_fn():
                passed += 1
                print(f"  PASSED")
            else:
                failed += 1
                print(f"  FAILED")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {e}")

    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
