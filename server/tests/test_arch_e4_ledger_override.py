"""Ledger-override tests (ARCH-E4, issue #150).

Deliberate reviewer directives live in the Preference Ledger scoped
to a block / scene / voice-role. When such a record carries a
loudness directive with PREFER / REQUIRE polarity, the uniform-LUFS
stylistic invariant (ARCH-E3) is suppressed for THAT scope only —
every other block still has the blanket check in force, and every
other invariant on the same block stays in force too.

This module verifies:

1. The :mod:`server.callbacks.virtual_brief` assembler is consulted
   at the narrowest matching scope first (block > role > scene >
   stage > global).
2. Polarities are honoured: PREFER / REQUIRE suppress, AVOID / FORBID
   do not.
3. Non-loudness ``VOICE`` records (e.g. "deeper tone") do NOT
   suppress the LUFS check — they're about timbre, not level.
4. Other invariants (peak limiter, voice continuity, character
   voice consistency) keep firing regardless of the override.
5. Hard conflicts on VOICE at an applicable scope raise
   :class:`RuntimeError` — we never silently pick a side.

Every test uses the real :mod:`server.callbacks.preference_ledger` API
(``append_preference``) to build a realistic blackboard.
"""

from __future__ import annotations

import math
import os
import wave

import numpy as np
import pytest

from callbacks.preference_ledger import (
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
)
from critique.audio_invariants import (
    InvariantVerdict,
    NarrationBlock,
    check_uniform_lufs,
    run_all_invariants,
)
from critique.ledger_override import (
    LOUDNESS_ASPECT_KEY,
    LOUDNESS_ASPECT_VALUE,
    build_lufs_override_resolver,
    is_lufs_override_active,
)


SR = 48_000


# ---------------------------------------------------------------------------
# Fixture helpers — reused from the E3 test suite style
# ---------------------------------------------------------------------------


def _write_wav(path: str, samples: np.ndarray, sr: int = SR) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(int16.tobytes())
    return path


def _voiced_sine(
    amplitude: float,
    freq: float = 220.0,
    duration: float = 1.5,
    sr: int = SR,
    *,
    seed: int = 0,
) -> np.ndarray:
    n = int(sr * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)
    sig = amplitude * np.sin(2 * math.pi * freq * t, dtype=np.float64)
    fade = int(0.05 * sr)
    if n > 2 * fade:
        window = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, fade)))
        sig[:fade] *= window
        sig[-fade:] *= window[::-1]
    sig += np.random.default_rng(seed).normal(scale=1e-4, size=n)
    return sig.astype(np.float32)


def _block(
    tmp_path,
    block_id: str,
    *,
    amplitude: float = 0.0708,
    scene_num: int = 1,
    voice_role: str = "V1",
    voice_id: str = "qwen3-tts:male_01",
) -> NarrationBlock:
    wav_path = str(tmp_path / f"{block_id}.wav")
    _write_wav(wav_path, _voiced_sine(amplitude))
    return NarrationBlock(
        block_id=block_id,
        wav_path=wav_path,
        scene_num=scene_num,
        voice_role=voice_role,
        language="ru",
        voice_id=voice_id,
    )


def _loud_block(tmp_path, block_id: str, **kwargs) -> NarrationBlock:
    """A block tuned to fail uniform LUFS (+8 dB hotter than target)."""
    return _block(tmp_path, block_id, amplitude=0.2, **kwargs)


def _origin(event_id: str = "evt-1") -> Origin:
    return Origin(
        l4_event_id=event_id,
        reviewer="reviewer@example.com",
        timestamp="2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Baseline — no ledger record means no override
# ---------------------------------------------------------------------------


class TestBaselineNoOverride:

    def test_empty_state_returns_false(self, tmp_path):
        state: dict = {}
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is False

    def test_ledger_without_voice_records_returns_false(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content="more dramatic",
            origin=_origin(),
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is False


# ---------------------------------------------------------------------------
# Scope resolution — block > role > scene > stage > global
# ---------------------------------------------------------------------------


class TestScopeResolution:

    def test_voice_block_scope_by_block_id_overrides(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="louder delivery by +3LU",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is True

    def test_voice_block_scope_by_voice_role_overrides(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="louder voice across the film",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is True

    def test_scene_scope_overrides_every_block_in_scene(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref="3",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="scene 3 is louder",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        in_scene = _block(tmp_path, "scene_003_V1", scene_num=3)
        out_scene = _block(tmp_path, "scene_001_V1", scene_num=1)
        assert is_lufs_override_active(state, in_scene) is True
        assert is_lufs_override_active(state, out_scene) is False

    def test_stage_scope_overrides_every_audio_block(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.STAGE,
            scope_ref="audio",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="whole film hotter mastered",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        b1 = _block(tmp_path, "scene_001_V1", scene_num=1)
        b2 = _block(tmp_path, "scene_007_V2", scene_num=7, voice_role="V2")
        assert is_lufs_override_active(state, b1) is True
        assert is_lufs_override_active(state, b2) is True

    def test_global_scope_overrides_every_block(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="film-wide louder reference",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is True

    def test_mismatched_scene_scope_does_not_override(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref="5",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="louder in scene 5",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1", scene_num=1)
        assert is_lufs_override_active(state, block) is False


# ---------------------------------------------------------------------------
# Polarity — PREFER/REQUIRE suppress; AVOID/FORBID don't
# ---------------------------------------------------------------------------


class TestPolarity:

    def test_require_polarity_suppresses(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.REQUIRE,
            subject=Subject.VOICE,
            content="MUST be louder in this block",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is True

    def test_avoid_polarity_does_not_suppress(self, tmp_path):
        # "Don't make it louder" is reinforcing the invariant, not
        # overriding it.
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.AVOID,
            subject=Subject.VOICE,
            content="keep loudness level",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is False

    def test_forbid_polarity_does_not_suppress(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.FORBID,
            subject=Subject.VOICE,
            content="MUST NOT make louder",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is False


# ---------------------------------------------------------------------------
# Subject filtering — only VOICE with loudness semantics overrides
# ---------------------------------------------------------------------------


class TestSubjectFiltering:

    def test_voice_record_without_loudness_aspect_keyword_is_ignored(
        self, tmp_path,
    ):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="deeper timbre, more gravel",  # no loudness words
            origin=_origin(),
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is False

    def test_keyword_fallback_recognises_plain_prose(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="make Cassandra louder in this beat",
            origin=_origin(),
            # no structured metadata — keyword fallback must catch.
        )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is True

    def test_non_voice_subjects_are_ignored(self, tmp_path):
        # TONE / PACING / VISUAL_STYLE records cannot suppress the
        # uniform-LUFS invariant, even with PREFER polarity.
        state: dict = {}
        for subject in (Subject.TONE, Subject.PACING, Subject.VISUAL_STYLE):
            append_preference(
                state,
                scope=Scope.GLOBAL,
                polarity=Polarity.PREFER,
                subject=subject,
                content="loudly",
                origin=_origin(),
            )
        block = _block(tmp_path, "scene_001_V1")
        assert is_lufs_override_active(state, block) is False


# ---------------------------------------------------------------------------
# Integration with the invariant pipeline (ARCH-E3)
# ---------------------------------------------------------------------------


class TestInvariantIntegration:

    def test_override_suppresses_uniform_lufs_check(self, tmp_path):
        block = _loud_block(tmp_path, "scene_001_V1")
        # Without override: fails uniform LUFS.
        no_override = check_uniform_lufs(block, override_active=False)
        assert no_override.verdict is InvariantVerdict.FAIL

        # With override: the invariant reports SKIP (override-suppressed).
        with_override = check_uniform_lufs(block, override_active=True)
        assert with_override.verdict is InvariantVerdict.SKIP

    def test_resolver_only_suppresses_target_block(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="louder in this scene only",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        target = _loud_block(tmp_path, "scene_001_V1")
        other = _loud_block(tmp_path, "scene_002_V1", scene_num=2)

        resolver = build_lufs_override_resolver(state)
        assert resolver(target) is True
        assert resolver(other) is False

        results = run_all_invariants(
            [target, other],
            override_resolver=resolver,
        )
        lufs_results = {
            r.block_id: r for r in results if r.name == "uniform_lufs"
        }
        assert lufs_results[target.block_id].verdict is InvariantVerdict.SKIP
        assert lufs_results[other.block_id].verdict is InvariantVerdict.FAIL

    def test_non_lufs_invariants_still_run_with_override_active(
        self, tmp_path,
    ):
        """The override suppresses uniform-LUFS ONLY. Every other
        invariant must still fire on the overridden block."""
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="louder reference for this block",
            origin=_origin(),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        # Use two same-speaker blocks so the adjacent-pair invariants
        # (voice_continuity, hiss_floor_continuity) fire too.
        block = _loud_block(tmp_path, "scene_001_V1", scene_num=1)
        neighbor = _loud_block(tmp_path, "scene_002_V1", scene_num=2)
        resolver = build_lufs_override_resolver(state)
        results = run_all_invariants(
            [block, neighbor], override_resolver=resolver,
        )

        # The same block still receives peak_limiter, voice_continuity,
        # character_voice_consistency, clicks, plosive_truncation,
        # hiss_floor_continuity results — the override only skips
        # uniform_lufs.
        check_names = {r.name for r in results}
        expected_always_on = {
            "peak_limiter",
            "voice_continuity",
            "character_voice_consistency",
            "clicks",
            "plosive_truncation",
            "hiss_floor",
        }
        assert expected_always_on.issubset(check_names)
        # Uniform LUFS still appears, but its verdict is SKIP on the
        # overridden block — the check ran, the override short-circuited it.
        lufs_for_block = next(
            r for r in results
            if r.name == "uniform_lufs"
            and r.block_id == block.block_id
        )
        assert lufs_for_block.verdict is InvariantVerdict.SKIP

    def test_no_override_records_pass_runs_uniform_lufs(self, tmp_path):
        """Sanity: an empty ledger must not accidentally suppress."""
        state: dict = {}
        block = _loud_block(tmp_path, "scene_001_V1")
        resolver = build_lufs_override_resolver(state)
        results = run_all_invariants([block], override_resolver=resolver)
        lufs_for_block = next(
            r for r in results
            if r.name == "uniform_lufs"
            and r.block_id == block.block_id
        )
        assert lufs_for_block.verdict is InvariantVerdict.FAIL


# ---------------------------------------------------------------------------
# Hard conflict handling
# ---------------------------------------------------------------------------


class TestHardConflict:

    def test_require_vs_forbid_on_same_scope_raises(self, tmp_path):
        state: dict = {}
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.REQUIRE,
            subject=Subject.VOICE,
            content="MUST be louder",
            origin=_origin("evt-1"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.FORBID,
            subject=Subject.VOICE,
            content="MUST NOT be louder",
            origin=_origin("evt-2"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1")
        with pytest.raises(RuntimeError, match="HARD CONFLICT"):
            is_lufs_override_active(state, block)


# ---------------------------------------------------------------------------
# Specificity — narrower scope wins over broader
# ---------------------------------------------------------------------------


class TestSpecificity:

    def test_block_prefer_beats_scene_avoid(self, tmp_path):
        # Scene-wide AVOID + block-specific PREFER → the block
        # override wins.
        state: dict = {}
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref="1",
            polarity=Polarity.AVOID,
            subject=Subject.VOICE,
            content="keep scene 1 uniform loudness",
            origin=_origin("evt-1"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="but THIS beat is louder",
            origin=_origin("evt-2"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1", scene_num=1)
        assert is_lufs_override_active(state, block) is True

    def test_stage_prefer_does_not_leak_past_refed_scene_avoid(self, tmp_path):
        # Regression (Devin Review): ``assemble_virtual_brief`` with
        # ``scope=VOICE_BLOCK`` pulls in broader GLOBAL / STAGE records
        # but excludes ref'd records from broader levels (e.g. a
        # ``SCENE scope_ref="3"`` record is NOT visible at a
        # VOICE_BLOCK query). Before the fix a STAGE PREFER would win
        # at the first VOICE_BLOCK query and the walk would stop —
        # the SCENE AVOID for scene 3 was never consulted, so the
        # override was wrongly active for blocks that the scene-level
        # AVOID explicitly said to keep uniform.
        state: dict = {}
        append_preference(
            state,
            scope=Scope.STAGE,
            scope_ref="audio",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="make this film LOUDER overall",
            origin=_origin("evt-1"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref="3",
            polarity=Polarity.AVOID,
            subject=Subject.VOICE,
            content="but keep scene 3 uniform",
            origin=_origin("evt-2"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        # Block in scene 3 — SCENE AVOID must dominate the STAGE
        # PREFER, so the override is SUPPRESSED.
        block_in_scene_3 = _block(tmp_path, "scene_003_V1", scene_num=3)
        assert is_lufs_override_active(state, block_in_scene_3) is False

        # Control: block in a different scene (no ref'd SCENE record
        # applies) — the STAGE PREFER wins and the override IS
        # active.
        block_in_scene_1 = _block(tmp_path, "scene_001_V1", scene_num=1)
        assert is_lufs_override_active(state, block_in_scene_1) is True

    def test_block_avoid_beats_scene_prefer(self, tmp_path):
        # Scene-wide PREFER + block-specific AVOID → the block wins;
        # override is SUPPRESSED at that scope.
        state: dict = {}
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref="1",
            polarity=Polarity.PREFER,
            subject=Subject.VOICE,
            content="scene 1 is louder",
            origin=_origin("evt-1"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        append_preference(
            state,
            scope=Scope.VOICE_BLOCK,
            scope_ref="scene_001_V1",
            polarity=Polarity.AVOID,
            subject=Subject.VOICE,
            content="but keep this exact block uniform",
            origin=_origin("evt-2"),
            metadata={LOUDNESS_ASPECT_KEY: LOUDNESS_ASPECT_VALUE},
        )
        block = _block(tmp_path, "scene_001_V1", scene_num=1)
        assert is_lufs_override_active(state, block) is False
