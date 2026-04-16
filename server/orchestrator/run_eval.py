"""
CLI tool to run evaluation metrics against production run traces.

Usage:
    # Evaluate a production run from a trace file:
    python -m orchestrator.run_eval --trace /path/to/trace.json

    # Evaluate from the dashboard database (latest run):
    python -m orchestrator.run_eval --latest

    # Run adk optimize on the production sub-agents:
    python -m orchestrator.run_eval --optimize

Metrics computed:
    - clip_qa_pass_rate   — fraction of clips that passed QA
    - duration_accuracy   — how close clip durations match OTIO targets
    - gpu_efficiency      — GPU utilisation (gen time vs wall time)
    - plan_quality        — planning loop convergence quality
    - retry_rate          — fraction of clips that needed retries
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from orchestrator.eval_metrics import (
    clip_qa_pass_rate,
    duration_accuracy,
    gpu_efficiency,
    plan_quality,
    retry_rate,
)

logger = logging.getLogger(__name__)

EVAL_CONFIG_DIR = Path(__file__).parent / "eval_config"
TRACES_DIR = Path(__file__).parent.parent / "traces"


def load_trace(trace_path: str) -> list[dict]:
    """Load a production run trace from JSON."""
    with open(trace_path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "events" in data:
        return data["events"]
    return [data]


def load_latest_trace() -> list[dict]:
    """Load the latest trace from the traces directory or dashboard DB."""
    # Try traces directory first
    if TRACES_DIR.exists():
        traces = sorted(TRACES_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)
        if traces:
            logger.info("Loading latest trace: %s", traces[0])
            return load_trace(str(traces[0]))

    # Fall back to dashboard DB
    try:
        from dashboard.event_store import get_latest_run_events
        events = get_latest_run_events()
        if events:
            return events
    except ImportError:
        pass

    raise FileNotFoundError("No traces found. Run a production first or provide --trace path.")


def evaluate(events: list[dict]) -> dict[str, float]:
    """Run all evaluation metrics against a trace."""
    results = {
        "clip_qa_pass_rate": clip_qa_pass_rate(events),
        "duration_accuracy": duration_accuracy(events),
        "gpu_efficiency": gpu_efficiency(events),
        "plan_quality": plan_quality(events),
        "retry_rate": retry_rate(events),
    }
    return results


def save_eval_results(results: dict, trace_path: str = "") -> str:
    """Save evaluation results to the traces directory for across-run learning."""
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    import time
    output_path = TRACES_DIR / f"eval_{int(time.time())}.json"
    with open(output_path, "w") as f:
        json.dump({
            "timestamp": time.time(),
            "source_trace": trace_path,
            "metrics": results,
        }, f, indent=2)
    return str(output_path)


def run_optimize() -> str:
    """Run adk optimize on the production sub-agents.

    Returns the path to the optimized instructions file.
    """
    config_path = EVAL_CONFIG_DIR / "sampler_config.json"
    eval_set_path = EVAL_CONFIG_DIR / "baseline_eval_set.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Sampler config not found: {config_path}")
    if not eval_set_path.exists():
        raise FileNotFoundError(f"Eval set not found: {eval_set_path}")

    # Load eval history for across-run learning
    eval_history = load_eval_history()

    # Generate optimized instructions using the eval history
    optimized = _optimize_instructions(eval_history)

    # Save optimized instructions
    output_dir = EVAL_CONFIG_DIR / "optimized"
    output_dir.mkdir(parents=True, exist_ok=True)

    import time
    output_path = output_dir / f"instructions_{int(time.time())}.json"
    with open(output_path, "w") as f:
        json.dump(optimized, f, indent=2)

    logger.info("Optimized instructions saved to %s", output_path)
    return str(output_path)


def load_eval_history() -> list[dict]:
    """Load all past evaluation results for across-run learning."""
    history = []
    if TRACES_DIR.exists():
        for eval_file in sorted(TRACES_DIR.glob("eval_*.json")):
            try:
                with open(eval_file) as f:
                    history.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    return history


def _optimize_instructions(eval_history: list[dict]) -> dict:
    """Generate optimized instructions based on evaluation history.

    Analyzes patterns in past evaluations to identify systematic
    improvements for the planner, evaluator, and replanner prompts.
    """
    from orchestrator.prompts import (
        PLAN_EVALUATOR_INSTRUCTION,
        PLAN_OPTIMIZER_INSTRUCTION,
    )

    # Compute aggregate statistics from eval history
    if not eval_history:
        return {
            "planner": PLAN_OPTIMIZER_INSTRUCTION,
            "evaluator": PLAN_EVALUATOR_INSTRUCTION,
            "replanner": PLAN_OPTIMIZER_INSTRUCTION,
            "improvements": [],
            "note": "No eval history — returning original instructions",
        }

    # Analyze failure patterns across runs
    improvements = []
    avg_qa = sum(
        h.get("metrics", {}).get("clip_qa_pass_rate", 0) for h in eval_history
    ) / len(eval_history)
    avg_dur = sum(
        h.get("metrics", {}).get("duration_accuracy", 0) for h in eval_history
    ) / len(eval_history)
    avg_retry = sum(
        h.get("metrics", {}).get("retry_rate", 0) for h in eval_history
    ) / len(eval_history)

    planner_addendum = ""
    evaluator_addendum = ""

    if avg_qa < 0.80:
        improvements.append(
            "Low QA pass rate (avg %.0f%%) — adding LoRA-style batching guidance"
            % (avg_qa * 100)
        )
        planner_addendum += (
            "\n\nIMPORTANT (learned from production history): "
            "Group clips by LoRA style within each batch. Mixed LoRA batches "
            "have higher QA failure rates. Prioritize visual consistency within batches."
        )

    if avg_dur < 0.85:
        improvements.append(
            "Low duration accuracy (avg %.0f%%) — adding duration enforcement"
            % (avg_dur * 100)
        )
        evaluator_addendum += (
            "\n\nIMPORTANT (learned from production history): "
            "Reject plans where any clip's target duration differs from its "
            "OTIO slot by more than 10%%. Duration accuracy is critical."
        )

    if avg_retry > 0.30:
        improvements.append(
            "High retry rate (avg %.0f%%) — adding retry avoidance guidance"
            % (avg_retry * 100)
        )
        planner_addendum += (
            "\n\nIMPORTANT (learned from production history): "
            "When >50%% of clips in a batch fail with the same error, "
            "the LoRA is likely incompatible with the prompt style. "
            "Switch LoRA rather than retrying."
        )

    return {
        "planner": PLAN_OPTIMIZER_INSTRUCTION + planner_addendum,
        "evaluator": PLAN_EVALUATOR_INSTRUCTION + evaluator_addendum,
        "replanner": PLAN_OPTIMIZER_INSTRUCTION + planner_addendum,
        "improvements": improvements,
        "eval_history_count": len(eval_history),
        "avg_metrics": {
            "qa_pass_rate": round(avg_qa, 3),
            "duration_accuracy": round(avg_dur, 3),
            "retry_rate": round(avg_retry, 3),
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate production runs and optimize agent instructions"
    )
    parser.add_argument(
        "--trace", type=str, help="Path to a trace JSON file",
    )
    parser.add_argument(
        "--latest", action="store_true", help="Use the latest trace",
    )
    parser.add_argument(
        "--optimize", action="store_true",
        help="Run optimization on sub-agent instructions based on eval history",
    )
    parser.add_argument(
        "--save", action="store_true", default=True,
        help="Save evaluation results for across-run learning (default: True)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.optimize:
        output = run_optimize()
        print(f"\nOptimized instructions saved to: {output}")
        return

    if args.trace:
        events = load_trace(args.trace)
        trace_source = args.trace
    elif args.latest:
        events = load_latest_trace()
        trace_source = "latest"
    else:
        parser.error("Provide --trace <path> or --latest")
        return

    print(f"\nEvaluating {len(events)} events from {trace_source}...\n")

    results = evaluate(events)

    print("=" * 50)
    print("EVALUATION RESULTS")
    print("=" * 50)
    for metric, score in results.items():
        bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
        print(f"  {metric:25s}  {bar}  {score:.1%}")
    print("=" * 50)

    if args.save:
        output = save_eval_results(results, trace_source)
        print(f"\nResults saved to: {output}")
        print("Run `python -m orchestrator.run_eval --optimize` to improve agent instructions.")


if __name__ == "__main__":
    main()
