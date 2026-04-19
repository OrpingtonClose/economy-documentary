"""Pytest runner for ADK eval golden files.

Discovers every ``*.evalset.json`` file under ``adk_eval/evalsets/`` and runs
it through the in-process ADK eval API (``AgentEvaluator.evaluate_eval_set``).

Rules:

* A file whose top-level ``metadata.stubbed`` is ``true`` is skipped with a
  clear reason. Stubs exist only to prove wiring; they are not meant to flake
  CI before a real golden has been captured via ``adk web``.
* A file that fails to parse as an ``EvalSet`` is reported as a pytest failure
  — treat it as a broken fixture.
* If no API key is available in the environment (``GOOGLE_API_KEY`` /
  ``OPENAI_API_KEY`` / ``OPENAI_API_BASE``), non-stubbed evals are skipped
  with a clear reason instead of hard-failing — the harness must work offline.

Real goldens captured through ``adk web`` should drop ``metadata.stubbed`` (or
set it to ``false``) to opt in to regression testing.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest


EVALSETS_DIR = Path(__file__).parent / "evalsets"
TEST_CONFIG_PATH = Path(__file__).parent / "test_config.json"
AGENT_MODULE = "adk_eval.agent"


def _discover_evalsets() -> list[Path]:
    if not EVALSETS_DIR.exists():
        return []
    return sorted(EVALSETS_DIR.glob("*.evalset.json"))


def _is_stubbed(evalset_path: Path) -> tuple[bool, str]:
    try:
        payload = json.loads(evalset_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{evalset_path.name}: invalid JSON ({exc})")
    metadata = payload.get("metadata") or {}
    stubbed = bool(metadata.get("stubbed"))
    reason = str(metadata.get("reason") or "marked stubbed: replace via `adk web` capture")
    return stubbed, reason


def _has_live_llm_credentials() -> bool:
    return bool(
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_BASE")
    )


@pytest.mark.parametrize(
    "evalset_path",
    _discover_evalsets(),
    ids=lambda p: p.name,
)
def test_evalset(evalset_path: Path) -> None:
    stubbed, reason = _is_stubbed(evalset_path)
    if stubbed:
        pytest.skip(f"{evalset_path.name}: {reason}")

    if not _has_live_llm_credentials():
        pytest.skip(
            f"{evalset_path.name}: no LLM credentials in env "
            "(GOOGLE_API_KEY / OPENAI_API_KEY / OPENAI_API_BASE)"
        )

    # Import lazily so a broken ADK install doesn't crash collection.
    from google.adk.evaluation.agent_evaluator import AgentEvaluator
    from google.adk.evaluation.eval_config import get_evaluation_criteria_or_default
    from google.adk.evaluation.eval_set import EvalSet

    eval_set = EvalSet.model_validate_json(evalset_path.read_text(encoding="utf-8"))
    eval_config = get_evaluation_criteria_or_default(str(TEST_CONFIG_PATH))

    asyncio.run(
        AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=eval_set,
            eval_config=eval_config,
            num_runs=1,
            print_detailed_results=True,
        )
    )


def test_evalsets_dir_exists() -> None:
    """Guardrail: the evalsets dir must exist even if everything inside is stubbed."""
    assert EVALSETS_DIR.is_dir(), f"missing {EVALSETS_DIR}"
