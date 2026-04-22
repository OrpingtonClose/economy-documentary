"""MemoryHonoringEvaluator — deterministic AGENTS.md honouring check.

Verifies that the DeepAgent orchestrator actually respected a test
invariant seeded into an AGENTS.md file before the run. See
``EVAL_ARCHITECTURE.md`` §7.3 for the seeding protocol.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records with at
  minimum a ``"name"`` key. Records may also carry ``"args"`` and
  ``"at_turn"``.
* ``metadata[`agents_md_before`]`` (required): the AGENTS.md content
  loaded into ``MemoryMiddleware`` at the start of the run.
* ``metadata[`agents_md_after`]`` (optional): the AGENTS.md content
  after the run completed. Used to verify the file was either left
  unchanged (acceptable) or edited to still-valid YAML (not torched).
* ``metadata[`forbidden_sequences`]`` (optional): list of
  ``{"before": str, "after": str}`` pairs. The evaluator asserts
  ``after`` never appears in ``actual_trajectory`` before ``before``
  has appeared at least once. Models the invariants we actually care
  about testing (e.g. "never call ``launch_assembly`` before
  ``launch_b2_sync`` has run").
* ``metadata[`required_tokens`]`` (optional): substrings that must be
  present in ``agents_md_after``. Used to check the orchestrator wrote
  its reflection back (when the playbook tells it to).

Output
------
One :class:`EvaluationOutput` per check performed. Hard gate: every
check must pass.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput


class MemoryHonoringEvaluator(Evaluator[Any, Any]):
    """Check that the orchestrator honoured seeded AGENTS.md invariants."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        agents_md_before = metadata.get("agents_md_before")
        agents_md_after = metadata.get("agents_md_after")
        forbidden_sequences = metadata.get("forbidden_sequences") or []
        required_tokens = metadata.get("required_tokens") or []

        if agents_md_before is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="metadata['agents_md_before'] is required",
                    label="memory.missing_seed",
                )
            ]

        trajectory = evaluation_case.actual_trajectory
        tool_names, turn_of_first_call = _index_trajectory(trajectory)
        outputs: list[EvaluationOutput] = []

        if forbidden_sequences and tool_names is None:
            outputs.append(
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory must be list[dict] to check ordering",
                    label="memory.missing_actual",
                )
            )
        else:
            for pair in forbidden_sequences:
                outputs.append(_check_ordering(pair, turn_of_first_call))

        for token in required_tokens:
            token_str = str(token)
            present = bool(agents_md_after) and token_str in str(agents_md_after)
            outputs.append(
                EvaluationOutput(
                    score=1.0 if present else 0.0,
                    test_pass=present,
                    reason=(
                        f"PASS AGENTS.md contains '{token_str}' after run"
                        if present
                        else f"FAIL AGENTS.md missing required token '{token_str}'"
                    ),
                    label="memory.required_token",
                )
            )

        if agents_md_after is not None:
            corrupted = _looks_corrupted(str(agents_md_after))
            outputs.append(
                EvaluationOutput(
                    score=0.0 if corrupted else 1.0,
                    test_pass=not corrupted,
                    reason=(
                        "FAIL AGENTS.md after run is empty or binary garbage"
                        if corrupted
                        else "PASS AGENTS.md after run is non-empty text"
                    ),
                    label="memory.integrity",
                )
            )

        if not outputs:
            outputs.append(
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no memory invariants configured for this case",
                    label="memory.noop",
                )
            )
        return outputs


def _index_trajectory(
    trajectory: Any,
) -> tuple[list[str] | None, dict[str, int]]:
    if not isinstance(trajectory, list):
        return None, {}
    names: list[str] = []
    first_turn: dict[str, int] = {}
    for idx, call in enumerate(trajectory):
        if not isinstance(call, dict):
            return None, {}
        name = call.get("name")
        if not isinstance(name, str):
            return None, {}
        names.append(name)
        at_turn = call.get("at_turn", idx)
        first_turn.setdefault(name, int(at_turn))
    return names, first_turn


def _check_ordering(
    pair: dict[str, str],
    turn_of_first_call: dict[str, int],
) -> EvaluationOutput:
    before = str(pair.get("before", ""))
    after = str(pair.get("after", ""))
    label = f"memory.order[{before}->{after}]"
    if not before or not after:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason=f"FAIL invalid forbidden_sequences entry: {pair}",
            label=label,
        )

    before_turn = turn_of_first_call.get(before)
    after_turn = turn_of_first_call.get(after)
    if after_turn is None:
        return EvaluationOutput(
            score=1.0,
            test_pass=True,
            reason=f"PASS '{after}' never called; invariant vacuous",
            label=label,
        )
    if before_turn is None:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason=f"FAIL '{after}' called without '{before}' ever running",
            label=label,
        )
    ok = before_turn <= after_turn
    return EvaluationOutput(
        score=1.0 if ok else 0.0,
        test_pass=ok,
        reason=(
            f"PASS '{before}' (turn {before_turn}) preceded '{after}' (turn {after_turn})"
            if ok
            else f"FAIL '{after}' (turn {after_turn}) called before '{before}' (turn {before_turn})"
        ),
        label=label,
    )


def _looks_corrupted(text: str) -> bool:
    if not text.strip():
        return True
    # ``str.encode("utf-8")`` succeeds for virtually every Python 3 string
    # (lone surrogates are the only exception), so it cannot be trusted to
    # spot binary data that was smuggled through ``bytes.decode(errors=...)``.
    # Instead, check for explicit markers of non-text payloads.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    if "\x00" in text:
        return True
    non_printable = sum(
        1 for c in text if not c.isprintable() and c not in "\n\r\t"
    )
    return non_printable / len(text) > 0.1
