"""Idiomatic ``__main__`` runner for :class:`Experiment` modules.

Every experiment under :mod:`strands_agents.evals.experiments` exposes
a ``build_*_experiment()`` factory and (where applicable) a
``*_task`` function. This module provides a single
:func:`run_experiment_as_main` helper those modules call from their
``if __name__ == "__main__":`` block so the invocation story is
uniform across the pipeline:

    python -m strands_agents.evals.experiments.<name>

Exit codes:

* ``0`` — every evaluator's ``test_passes`` is fully True.
* ``1`` — one or more evaluator case-level tests failed.
* ``2`` — the experiment could not run (missing credentials, missing
  fixtures, empty case list, etc.). Distinguishing skip from failure
  lets the nightly workflow mark an experiment as *degraded* without
  reporting a red regression.

The runner is intentionally dependency-light: no CLI framework, no
argument parsing. The only knobs are what the caller passes. Anything
more elaborate belongs in a wrapper script, not here.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation_report import EvaluationReport

# Exit codes are part of the public contract — CI workflows key on
# them to distinguish pass/fail/skip. Kept named so the workflow
# YAML can reference the same values.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 2


def _case_name(case_entry: Any, index: int) -> str:
    """Resolve a case's display name from the parallel ``cases`` list."""
    if isinstance(case_entry, dict):
        name = case_entry.get("name") or case_entry.get("case_name")
        if name:
            return str(name)
    return f"case_{index}"


def _format_report(report: EvaluationReport) -> str:
    """Render one :class:`EvaluationReport` as a human-readable block."""
    lines: list[str] = []
    passes = list(report.test_passes or [])
    reasons = list(report.reasons or [])
    cases = list(report.cases or [])
    passed = sum(1 for v in passes if v)
    total = len(passes)
    lines.append(
        f"[{report.evaluator_name}] "
        f"overall_score={report.overall_score:.3f} "
        f"passed={passed}/{total}"
    )
    # Failing cases first, then passing. Truncate reasons so a giant
    # trajectory dump doesn't drown out the summary.
    triples = [
        (_case_name(cases[i] if i < len(cases) else None, i),
         passes[i],
         reasons[i] if i < len(reasons) else "")
        for i in range(total)
    ]
    for case_name, did_pass, reason in sorted(triples, key=lambda t: (t[1], t[0])):
        reason_text = (reason or "").strip().replace("\n", " ")
        if len(reason_text) > 160:
            reason_text = reason_text[:157] + "..."
        status = "PASS" if did_pass else "FAIL"
        lines.append(f"  {status} {case_name}: {reason_text}")
    return "\n".join(lines)


def _json_summary(reports: list[EvaluationReport]) -> dict[str, Any]:
    """Machine-readable summary for CI artefact upload."""
    out: list[dict[str, Any]] = []
    for r in reports:
        passes = list(r.test_passes or [])
        reasons = list(r.reasons or [])
        cases = list(r.cases or [])
        test_passes: dict[str, bool] = {}
        case_reasons: dict[str, str] = {}
        for i, did_pass in enumerate(passes):
            name = _case_name(cases[i] if i < len(cases) else None, i)
            test_passes[name] = bool(did_pass)
            reason = reasons[i] if i < len(reasons) else ""
            case_reasons[name] = (reason or "")[:500]
        out.append(
            {
                "name": r.evaluator_name,
                "overall_score": r.overall_score,
                "test_passes": test_passes,
                "reasons": case_reasons,
            }
        )
    return {"evaluators": out}


def run_experiment_as_main(
    build_fn: Callable[[], Experiment],
    task_fn: Callable[[Case], Any],
    *,
    required_env: tuple[str, ...] = (),
    name: str | None = None,
) -> int:
    """Run an experiment and print an exit-code-gated summary.

    Call this from an experiment module's ``__main__`` block. It
    builds the experiment, invokes :meth:`Experiment.run_evaluations`
    with ``task_fn``, prints both human-readable and JSON summaries
    to stdout, and returns the exit code the caller should
    ``sys.exit`` with.

    Args:
        build_fn: Zero-arg factory returning the :class:`Experiment`.
        task_fn: Task callable passed to
            :meth:`Experiment.run_evaluations`. Same signature
            contract as the framework expects.
        required_env: Environment variable names that must be set
            (non-empty) for the experiment to run. If any are missing
            the runner returns :data:`EXIT_SKIP` without invoking
            the factory — distinguishes "no credentials" from
            "credentials but failure".
        name: Display name. Defaults to ``build_fn.__module__``.

    Returns:
        :data:`EXIT_PASS` if every evaluator's ``test_passes`` is
        fully True, :data:`EXIT_FAIL` if any are False,
        :data:`EXIT_SKIP` if required env is missing or the
        experiment has no cases.
    """
    display = name or build_fn.__module__
    missing = [var for var in required_env if not os.environ.get(var)]
    if missing:
        print(
            f"SKIP {display}: missing required env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        return EXIT_SKIP

    try:
        experiment = build_fn()
    except ValueError as exc:
        # An experiment that refuses to build (e.g. no closed pairs)
        # is a skip, not a fail — failing would cause red CI on a
        # corpus gap rather than on a real regression.
        print(f"SKIP {display}: {exc}", file=sys.stderr)
        return EXIT_SKIP

    if not experiment.cases:
        print(f"SKIP {display}: experiment has no cases", file=sys.stderr)
        return EXIT_SKIP

    # Some experiments (typically evaluator-self-tests) deliberately
    # include cases designed to *fail* the evaluator — the negative
    # half of a paired contract test. Such cases carry
    # ``metadata={"expect_pass": False}`` so the runner can invert
    # the gate per-case instead of treating every False as a
    # regression.
    expected_per_case: dict[str, bool] = {}
    for case in experiment.cases:
        meta = getattr(case, "metadata", None) or {}
        expected_per_case[case.name] = bool(meta.get("expect_pass", True))

    reports = experiment.run_evaluations(task=task_fn)

    print(f"=== {display} ===")
    for report in reports:
        print(_format_report(report))

    # JSON trailer lets CI parse the outcome without screen-scraping
    # the human-readable block. A workflow can grep for ``"summary":``
    # and jq into it.
    print("summary:", json.dumps(_json_summary(reports), sort_keys=True))

    all_green = True
    for report in reports:
        passes = list(report.test_passes or [])
        cases = list(report.cases or [])
        for i, did_pass in enumerate(passes):
            case_name = _case_name(cases[i] if i < len(cases) else None, i)
            expected = expected_per_case.get(case_name, True)
            if bool(did_pass) != expected:
                all_green = False
                break
        if not all_green:
            break
    return EXIT_PASS if all_green else EXIT_FAIL
