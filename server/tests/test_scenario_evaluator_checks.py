"""
Unit tests for the structural scenario evaluator checks.

Each check gets a passing example and at least one failing example drawn
from the PAG production run's actual defect modes (duration shortfall,
rhetorical questions, missing initialisms, visual whiplash, generic hook,
missing outro).

Run from ``server/`` with::

    poetry run pytest tests/test_scenario_evaluator_checks.py

or directly with::

    python -m pytest server/tests/test_scenario_evaluator_checks.py
"""

from __future__ import annotations

import os
import sys

# Add server/ to sys.path so ``from contracts import ...`` works when the
# test file is invoked directly or from a repo-root pytest run.
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import pytest  # noqa: E402

from tools.scenario_evaluator_checks import (  # noqa: E402
    CheckResult,
    EvaluatorReport,
    TopicClassification,
    cap_verdict,
    check_duration_compliance,
    check_hook_spec_present,
    check_no_rhetorical_questions,
    check_outro_spec_present,
    check_pronunciation_hints_coverage,
    check_scene_count,
    check_style_consistency,
    check_style_lock_present,
    check_topic_fidelity,
    check_word_count,
    collect_narration,
    format_report,
    run_all_structural_checks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _voice(voice: str, text: str) -> dict:
    return {"voice": voice, "text": text, "tone": "neutral"}


def _make_scene(
    n: int,
    duration: float,
    v1_text: str,
    v2_text: str,
    v3_text: str,
    title: str = "Scene",
    visual: str = "wide establishing shot",
    dopamine: str = "surprising fact up-front",
    **kwargs,
) -> dict:
    scene = {
        "scene_num": n,
        "title": title,
        "duration_sec": duration,
        "voices": [
            _voice("V1", v1_text),
            _voice("V2", v2_text),
            _voice("V3", v3_text),
        ],
        "visual_notes": visual,
        "dopamine_hook": dopamine,
    }
    scene.update(kwargs)
    return scene


_FILLER = (
    "The periaqueductal gray sits deep in the midbrain and coordinates the body's response to "
    "pain, fear, and vocalisation. Researchers have traced projections from this small nucleus "
    "to dozens of downstream targets, ranging from the dorsal horn of the spinal cord to the "
    "amygdala. Understanding its circuitry has become central to modern neuroscience."
)


def _make_good_scenario(num_scenes: int = 10, scene_duration: float = 45.0) -> dict:
    """Build a scenario that should pass every structural check."""
    scenes = []
    for i in range(num_scenes):
        scenes.append(
            _make_scene(
                n=i + 1,
                duration=scene_duration,
                v1_text=(
                    f"Scene {i + 1} hook. {_FILLER} This opens the beat with a declarative "
                    "statement about the periaqueductal gray and its role in analgesia."
                ),
                v2_text=(
                    f"Expert block {i + 1}. The P-A-G integrates descending pain modulation "
                    "across multiple layers of the spinal cord. Empirical data from the 2020s "
                    "shows measurable analgesic effects from targeted D-B-S intervention."
                ),
                v3_text=(
                    f"Storyteller block {i + 1}. A patient with chronic pain describes what it "
                    "feels like when electrical stimulation of the periaqueductal gray quiets "
                    "the daily background noise of suffering."
                ),
                title=f"Scene {i + 1}",
                visual="slow push-in on a laboratory bench",
                dopamine="counterintuitive statistic opens the beat",
                pronunciation_hints={"PAG": "P-A-G", "DBS": "D-B-S"},
            )
        )

    # Scene 0: hook_spec
    scenes[0]["hook_spec"] = {
        "topic_specific_motif": "a single periaqueductal gray neuron on a microscope slide",
        "motion_description": "slow push-in with shallow depth of field",
        "narrative_pull": "within 7 seconds the viewer learns this neuron can silence pain",
    }
    # Final scene: outro_spec
    scenes[-1]["outro_spec"] = {
        "closing_shot": "wide shot of the empty operating theatre, lights dim",
        "recap_sentence": "The periaqueductal gray is how the brain negotiates with pain.",
        "cta": "subscribe for the next episode on deep brain stimulation",
        "brand_card": "Economy Documentary — The PAG Episode",
    }

    return {
        "scenes": scenes,
        "style_lock": {
            "dominant_style": "cinematic_documentary",
            "positive_fragment": "cinematic documentary, photoreal, natural lighting, 4K",
            "negative_fragment": "anime, cartoon, morphing, distorted anatomy",
            "forbidden_styles": ["anime", "cartoon", "watercolor", "cyberpunk"],
        },
        "pronunciation_hints": {"PAG": "P-A-G", "DBS": "D-B-S"},
    }


# ---------------------------------------------------------------------------
# cap_verdict
# ---------------------------------------------------------------------------


def test_cap_verdict_picks_stricter():
    assert cap_verdict("EXCELLENT", "POOR") == "POOR"
    assert cap_verdict("GOOD", "EXCELLENT") == "GOOD"
    assert cap_verdict("POOR", "EXCELLENT") == "POOR"
    assert cap_verdict("FAIR", "GOOD") == "FAIR"


def test_cap_verdict_unknown_defaults_to_excellent():
    # Unknown strings normalize to EXCELLENT rank so they don't corrupt
    # the cap — caller is expected to use the canonical ladder.
    assert cap_verdict("WEIRD", "POOR") == "POOR"


# ---------------------------------------------------------------------------
# check_duration_compliance
# ---------------------------------------------------------------------------


def test_duration_compliance_passes_exact_target():
    scenes = [_make_scene(i + 1, 45.0, "x", "y", "z") for i in range(10)]
    r = check_duration_compliance(scenes, 420.0)
    assert r.passed
    assert r.data["sum_duration_sec"] == 450.0


def test_duration_compliance_pag_shortfall_fails():
    # PAG run: user said 7 minutes (420s), pipeline produced 3:50 (230s).
    scenes = [_make_scene(i + 1, 45.0, "x", "y", "z") for i in range(5)]
    r = check_duration_compliance(scenes, 420.0)
    assert not r.passed
    assert r.verdict_cap == "POOR"
    assert r.data["shortfall_pct"] > 30.0


def test_duration_compliance_within_tolerance_passes():
    scenes = [_make_scene(i + 1, 40.0, "x", "y", "z") for i in range(10)]  # 400s
    r = check_duration_compliance(scenes, 420.0, tolerance=0.05)
    assert r.passed


def test_duration_compliance_zero_target_skips():
    scenes = [_make_scene(1, 30.0, "a", "b", "c")]
    r = check_duration_compliance(scenes, 0.0)
    assert r.passed


# ---------------------------------------------------------------------------
# check_scene_count
# ---------------------------------------------------------------------------


def test_scene_count_enough_scenes_passes():
    scenes = [_make_scene(i + 1, 45.0, "x", "y", "z") for i in range(10)]
    r = check_scene_count(scenes, 420.0)
    assert r.passed


def test_scene_count_too_few_fails():
    scenes = [_make_scene(i + 1, 90.0, "x", "y", "z") for i in range(3)]  # 3 scenes, 7min target
    r = check_scene_count(scenes, 420.0)
    assert not r.passed
    assert r.data["minimum"] >= 10


# ---------------------------------------------------------------------------
# check_word_count
# ---------------------------------------------------------------------------


def test_word_count_enough_words_passes():
    scenario = _make_good_scenario(num_scenes=10, scene_duration=45.0)
    r = check_word_count(scenario["scenes"], 420.0, wpm=150)
    assert r.passed


def test_word_count_too_few_words_fails():
    scenes = [_make_scene(i + 1, 45.0, "a", "b", "c") for i in range(10)]  # tiny text
    r = check_word_count(scenes, 420.0, wpm=150)
    assert not r.passed
    assert r.verdict_cap == "POOR"


# ---------------------------------------------------------------------------
# check_hook_spec_present
# ---------------------------------------------------------------------------


def test_hook_spec_passes_when_topic_specific():
    scenario = _make_good_scenario()
    r = check_hook_spec_present(scenario["scenes"], user_prompt="periaqueductal gray")
    assert r.passed


def test_hook_spec_missing_fails():
    scenario = _make_good_scenario()
    del scenario["scenes"][0]["hook_spec"]
    r = check_hook_spec_present(scenario["scenes"])
    assert not r.passed
    assert "missing hook_spec" in r.details


def test_hook_spec_too_short_motif_fails():
    # PAG run: hook was "blurry 3D brain" — too generic.
    scenes = [_make_scene(1, 45, "x", "y", "z", hook_spec={
        "topic_specific_motif": "brain",
        "motion_description": "pan",
        "narrative_pull": "viewer watches",
    })]
    r = check_hook_spec_present(scenes)
    assert not r.passed


def test_hook_spec_empty_motion_fails():
    scenes = [_make_scene(1, 45, "x", "y", "z", hook_spec={
        "topic_specific_motif": "an electrode array on silicon",
        "motion_description": "",
        "narrative_pull": "viewer stays for the reveal",
    })]
    r = check_hook_spec_present(scenes)
    assert not r.passed


# ---------------------------------------------------------------------------
# check_outro_spec_present
# ---------------------------------------------------------------------------


def test_outro_spec_passes():
    scenario = _make_good_scenario()
    r = check_outro_spec_present(scenario["scenes"])
    assert r.passed


def test_outro_spec_missing_fails():
    # PAG run: documentary ended on a fade with no outro.
    scenario = _make_good_scenario()
    del scenario["scenes"][-1]["outro_spec"]
    r = check_outro_spec_present(scenario["scenes"])
    assert not r.passed


def test_outro_spec_missing_cta_fails():
    scenario = _make_good_scenario()
    scenario["scenes"][-1]["outro_spec"]["cta"] = ""
    r = check_outro_spec_present(scenario["scenes"])
    assert not r.passed


# ---------------------------------------------------------------------------
# check_style_lock_present / check_style_consistency
# ---------------------------------------------------------------------------


def test_style_lock_present_passes():
    scenario = _make_good_scenario()
    r = check_style_lock_present(scenario)
    assert r.passed
    assert r.data["dominant_style"] == "cinematic_documentary"


def test_style_lock_missing_fails():
    # PAG run: no style lock → visual whiplash.
    r = check_style_lock_present({"scenes": []})
    assert not r.passed


def test_style_lock_empty_positive_fragment_fails():
    r = check_style_lock_present(
        {"style_lock": {"dominant_style": "cinematic_documentary", "positive_fragment": ""}}
    )
    assert not r.passed


def test_style_consistency_passes_when_no_forbidden_keywords():
    scenario = _make_good_scenario()
    r = check_style_consistency(scenario["scenes"], scenario["style_lock"])
    assert r.passed


def test_style_consistency_flags_anime_in_visual_notes():
    # PAG run: visual_notes mixed anime + watercolor + cyberpunk.
    scenario = _make_good_scenario()
    scenario["scenes"][2]["visual_notes"] = "anime-style cityscape with watercolor sky"
    r = check_style_consistency(scenario["scenes"], scenario["style_lock"])
    assert not r.passed
    assert any(v["scene_num"] == 3 for v in r.data["violations"])


# ---------------------------------------------------------------------------
# check_pronunciation_hints_coverage
# ---------------------------------------------------------------------------


def test_pronunciation_hints_cover_all_initialisms():
    narration = "The P-A-G projects to the spinal cord. Clinical trials use DBS and fMRI."
    r = check_pronunciation_hints_coverage(
        narration, {"DBS": "D-B-S", "fMRI": "f-M-R-I"}
    )
    assert r.passed  # P-A-G is already dashed, DBS and fMRI covered
    # Note: fMRI's 'MRI' substring is all-caps 3-letter — it appears as a token.
    # Let's verify with a cleaner example.


def test_pronunciation_hints_missing_pag_fails():
    # PAG run: narration used "PAG" with no hint → TTS said "pag".
    narration = "The PAG is a small midbrain nucleus. PAG neurons project widely."
    r = check_pronunciation_hints_coverage(narration, {})
    assert not r.passed
    assert "PAG" in r.data["missing"]


def test_pronunciation_hints_whitelist_allows_obvious_english():
    # "OK" and "THE" should be ignored.
    narration = "OK THE PAG is deep in the midbrain."
    r = check_pronunciation_hints_coverage(narration, {"PAG": "P-A-G"})
    assert r.passed


def test_pronunciation_hints_accepts_hyphenated_tokens_as_clean():
    # Once the LLM has rewritten PAG → P-A-G, the all-caps regex should
    # not match P-A-G as a single token (letters are 1 char each).
    narration = "The P-A-G projects descending fibres."
    r = check_pronunciation_hints_coverage(narration, {})
    assert r.passed


# ---------------------------------------------------------------------------
# check_no_rhetorical_questions
# ---------------------------------------------------------------------------


def test_no_rhetorical_passes_on_declarative_narration():
    r = check_no_rhetorical_questions(
        "The PAG is a small midbrain nucleus. It coordinates analgesia and defensive behaviour."
    )
    assert r.passed


def test_rhetorical_catches_what_happens_when():
    # PAG run: "What happens when...?" slipped past evaluator.
    r = check_no_rhetorical_questions(
        "The PAG modulates pain. What happens when we stimulate it directly?"
    )
    assert not r.passed
    assert r.data["rhetorical"]


def test_rhetorical_catches_can_we_harness():
    # PAG run: "Can we harness...?" also slipped past.
    r = check_no_rhetorical_questions(
        "Research continues. Can we harness this circuit for clinical analgesia?"
    )
    assert not r.passed


def test_rhetorical_can_be_overridden_by_classifier():
    # Interview segments sometimes contain direct questions that should
    # pass.  The caller supplies an LLM classifier.
    def classify(_q: str) -> str:
        return "direct"

    r = check_no_rhetorical_questions(
        "What is your daily routine like? describes the subject.",
        classify=classify,
    )
    assert r.passed


# ---------------------------------------------------------------------------
# check_topic_fidelity
# ---------------------------------------------------------------------------


def test_topic_fidelity_passes_on_on_topic_classifier():
    scenario = _make_good_scenario(num_scenes=5)

    def classify(_prompt: str, _scene_text: str) -> TopicClassification:
        return TopicClassification(scene_num=0, verdict="on_topic")

    r = check_topic_fidelity(scenario["scenes"], "periaqueductal gray", classify=classify)
    assert r.passed


def test_topic_fidelity_fails_when_many_off_topic():
    # PAG run: EU parliament + cyberpunk + prayer = 40% off-topic.
    scenario = _make_good_scenario(num_scenes=10)

    verdicts = ["on_topic"] * 6 + ["off_topic"] * 4  # 40% off-topic
    calls = {"i": 0}

    def classify(_prompt: str, _scene_text: str) -> TopicClassification:
        i = calls["i"]
        calls["i"] += 1
        return TopicClassification(scene_num=i, verdict=verdicts[i])

    r = check_topic_fidelity(scenario["scenes"], "periaqueductal gray", classify=classify)
    assert not r.passed
    assert len(r.data["off_topic_scenes"]) == 4


def test_topic_fidelity_tolerates_single_off_topic():
    scenario = _make_good_scenario(num_scenes=10)
    verdicts = ["on_topic"] * 9 + ["off_topic"]
    calls = {"i": 0}

    def classify(_p: str, _t: str) -> TopicClassification:
        i = calls["i"]
        calls["i"] += 1
        return TopicClassification(scene_num=i, verdict=verdicts[i])

    r = check_topic_fidelity(
        scenario["scenes"],
        "periaqueductal gray",
        classify=classify,
        off_topic_threshold=1,
        tangential_pct_threshold=0.30,
    )
    assert r.passed


def test_topic_fidelity_no_classifier_caps_at_good():
    scenario = _make_good_scenario(num_scenes=5)
    r = check_topic_fidelity(scenario["scenes"], "periaqueductal gray")
    # With no classifier, we can't confirm the scenario is on-topic.  We
    # return passed=True but cap at GOOD (not EXCELLENT) and say so.
    assert r.passed
    assert r.verdict_cap == "GOOD"


# ---------------------------------------------------------------------------
# run_all_structural_checks — aggregate
# ---------------------------------------------------------------------------


def test_run_all_passes_on_good_scenario():
    scenario = _make_good_scenario()

    def topic_classify(_p: str, _t: str) -> TopicClassification:
        return TopicClassification(scene_num=0, verdict="on_topic")

    report = run_all_structural_checks(
        scenario,
        user_prompt="periaqueductal gray",
        target_duration_sec=420.0,
        topic_classify=topic_classify,
    )
    failed = report.failed()
    assert not failed, f"unexpected failures: {[f.name + ': ' + f.details for f in failed]}"
    assert report.overall == "EXCELLENT"


def test_run_all_caps_at_poor_on_pag_defect_profile():
    # Reproduce the PAG production run's actual defects:
    # - duration shortfall (3:50 of 7min)
    # - no style_lock
    # - no hook_spec
    # - no outro_spec
    # - rhetorical questions in narration
    # - PAG used without pronunciation hint
    scenes = [
        _make_scene(
            1, 45.0,
            "The PAG is a small midbrain nucleus. What happens when we stimulate it?",
            "Research is ongoing. " + _FILLER,
            "A patient recounts chronic pain. " + _FILLER,
        ),
        _make_scene(
            2, 45.0,
            "PAG neurons project widely. Can we harness this for therapy?",
            "Data suggests yes. " + _FILLER,
            "Another patient shares their story. " + _FILLER,
        ),
        _make_scene(
            3, 45.0,
            "More research follows. PAG continues to surprise investigators.",
            "Clinical trials progress. " + _FILLER,
            "Hope for the future. " + _FILLER,
        ),
    ]
    # 3:50 = 230s total; user asked for 7min = 420s.
    for i, s in enumerate(scenes):
        s["duration_sec"] = [80, 80, 70][i]

    scenario = {"scenes": scenes}  # NO style_lock, NO hooks, NO outro

    def topic_classify(_p: str, _t: str) -> TopicClassification:
        return TopicClassification(scene_num=0, verdict="on_topic")

    report = run_all_structural_checks(
        scenario,
        user_prompt="periaqueductal gray",
        target_duration_sec=420.0,
        topic_classify=topic_classify,
    )
    assert report.overall == "POOR"

    names_failed = {r.name for r in report.failed()}
    # Every one of these PAG defect modes should fire:
    assert "duration_compliance" in names_failed
    assert "scene_count" in names_failed
    assert "word_count" in names_failed
    assert "hook_spec_present" in names_failed
    assert "outro_spec_present" in names_failed
    assert "style_lock_present" in names_failed
    assert "pronunciation_hints_coverage" in names_failed
    assert "no_rhetorical_questions" in names_failed


def test_format_report_includes_failures():
    report = EvaluatorReport(
        overall="POOR",
        results=[
            CheckResult(name="test_check", passed=False, verdict_cap="POOR", details="bad"),
            CheckResult(name="other_check", passed=True, verdict_cap="EXCELLENT", details="ok"),
        ],
    )
    out = format_report(report)
    assert "OVERALL_CAP: POOR" in out
    assert "[FAIL (cap=POOR)] test_check" in out
    assert "PASS" in out
    assert "FAILURES REQUIRE REVISION" in out


# ---------------------------------------------------------------------------
# collect_narration
# ---------------------------------------------------------------------------


def test_collect_narration_strips_language_tags():
    scenes = [
        _make_scene(
            1, 45,
            "[RU] Русский текст\n[EN] English text",
            "[RU] Еще\n[EN] More",
            "[RU] И еще\n[EN] And more",
        )
    ]
    narration = collect_narration(scenes)
    assert "[RU]" not in narration
    assert "[EN]" not in narration
    assert "English text" in narration


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
