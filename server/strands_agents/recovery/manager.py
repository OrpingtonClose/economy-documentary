from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from .models import (
    format_recovery_status_summary,
    FailureClass,
    RecoveryAction,
    RecoveryBudget,
    RecoveryDecision,
)

logger = logging.getLogger(__name__)


class RecoveryManager:
    """
    Manages recovery budgets and decides recovery actions.

    Designed to be used by the orchestrator and media agents.
    """

    def __init__(self, alert_dir: str | Path | None = None) -> None:
        self._budgets: dict[int, RecoveryBudget] = {}  # scene_num -> budget
        self._run_level_attempts: dict[FailureClass, int] = {
            FailureClass.INFRA: 0,
            FailureClass.CONTENT: 0,
            FailureClass.SEMANTIC: 0,
        }
        self._recovery_outcomes: dict[int, list[dict[str, Any]]] = {}  # scene_num -> list of outcomes
        self._alert_dir = Path(alert_dir) if alert_dir else Path("recovery_alerts")

    def get_or_create_budget(self, scene_num: int) -> RecoveryBudget:
        if scene_num not in self._budgets:
            self._budgets[scene_num] = RecoveryBudget(scene_num=scene_num)
        return self._budgets[scene_num]

    def classify_failure(self, error: str, artifact_type: str) -> FailureClass:
        """Very basic classifier. Will be improved with LLM assistance later."""
        error_lower = error.lower()

        if any(kw in error_lower for kw in ["worker", "reclaimed", "provision", "network", "b2", "timeout"]):
            return FailureClass.INFRA

        if any(kw in error_lower for kw in ["morph", "wonk", "bad voice", "unpleasant"]):
            return FailureClass.CONTENT

        if any(kw in error_lower for kw in ["does not match", "semantic", "wrong action", "wrong framing"]):
            return FailureClass.SEMANTIC

        return FailureClass.UNKNOWN

    def handle_failure(
        self,
        scene_num: int,
        error_message: str,
        artifact_type: str,
        extra_context: dict[str, Any] | None = None,
    ) -> RecoveryDecision:
        """
        High-level entry point for agents when something fails.

        This is the method media agents (audio, production) should call
        when a render or quality check fails.
        """
        failure_class = self.classify_failure(error_message, artifact_type)
        budget = self.get_or_create_budget(scene_num)

        if not budget.can_attempt(failure_class):
            logger.critical(
                f"Scene {scene_num}: Recovery budget exhausted for {failure_class.value}. Escalating."
            )
            self._write_escalation_alert(
                scene_num=scene_num,
                failure_class=failure_class,
                error_message=error_message,
                artifact_type=artifact_type,
                reason=f"Budget exhausted for {failure_class.value}",
                extra_context=extra_context,
            )
            return RecoveryDecision(
                action=RecoveryAction.ESCALATE,
                reason=self._build_escalation_reason(
                    scene_num, failure_class, error_message, artifact_type,
                    f"Budget exhausted for {failure_class.value}",
                    extra_context,
                ),
            )

        # Check recent recovery history before deciding
        if self._has_repeated_failures(scene_num, failure_class):
            logger.critical(f"Scene {scene_num}: Multiple recent failed recoveries for {failure_class.value}. Escalating.")
            self._write_escalation_alert(
                scene_num=scene_num,
                failure_class=failure_class,
                error_message=error_message,
                artifact_type=artifact_type,
                reason=f"Repeated failures for {failure_class.value}",
                extra_context=extra_context,
            )
            return RecoveryDecision(
                action=RecoveryAction.ESCALATE,
                reason=self._build_escalation_reason(
                    scene_num, failure_class, error_message, artifact_type,
                    f"Repeated failures for {failure_class.value}",
                    extra_context,
                ),
            )

        # Run-level hard cap (global safety net) — the ultimate backstop for unattended runs
        total_run_attempts = sum(self._run_level_attempts.values())
        if total_run_attempts >= 50:
            logger.critical(f"Run-level recovery attempt cap reached ({total_run_attempts}). Forcing hard escalation.")

            # Record an explicit outcome so the per-scene history and get_recovery_history
            # tools show a clear audit trail of why this failure triggered the global cap.
            self.record_recovery_outcome(
                scene_num=scene_num,
                action=RecoveryAction.ESCALATE,
                success=False,
                details={
                    "failure_class": failure_class.value,
                    "error": error_message,
                    "artifact_type": artifact_type,
                    "reason": "GLOBAL_RECOVERY_CAP_EXCEEDED",
                    "total_run_attempts": total_run_attempts,
                }
            )

            # Also bump the run-level counter so the cap remains enforced on subsequent calls
            self._run_level_attempts[failure_class] = self._run_level_attempts.get(failure_class, 0) + 1

            # Save a checkpoint of recovery state before escalating so the run can be resumed / inspected
            self._save_checkpoint(
                scene_num=scene_num,
                reason="Run-level recovery attempt cap exceeded",
            )

            self._write_escalation_alert(
                scene_num=scene_num,
                failure_class=failure_class,
                error_message=error_message,
                artifact_type=artifact_type,
                reason="Run-level recovery attempt cap exceeded",
                extra_context=extra_context,
            )

            return RecoveryDecision(
                action=RecoveryAction.ESCALATE,
                reason=self._build_escalation_reason(
                    scene_num, failure_class, error_message, artifact_type,
                    "Run-level recovery attempt cap exceeded",
                    extra_context,
                ),
            )

        # NEW: Explicit thrashing detection (same action failing repeatedly)
        thrashing_action = self.detect_thrashing(scene_num)
        if thrashing_action is not None:
            logger.critical(f"Scene {scene_num}: Thrashing detected (repeated same recovery action). Forcing {thrashing_action.value}.")
            self._write_escalation_alert(
                scene_num=scene_num,
                failure_class=failure_class,
                error_message=error_message,
                artifact_type=artifact_type,
                reason="Recovery thrashing detected: repeated identical recovery actions without success",
                extra_context=extra_context,
            )
            return RecoveryDecision(
                action=thrashing_action,
                reason=self._build_escalation_reason(
                    scene_num, failure_class, error_message, artifact_type,
                    "Recovery thrashing detected: repeated identical recovery actions without success",
                    extra_context,
                ),
            )

        # Normal recovery path
        self._run_level_attempts[failure_class] = self._run_level_attempts.get(failure_class, 0) + 1
        return RecoveryDecision(
            action=RecoveryAction.RETRY,
            reason=f"Attempting recovery for {failure_class.value} failure on {artifact_type}",
        )

    def _has_repeated_failures(self, scene_num: int, failure_class: FailureClass) -> bool:
        outcomes = self._recovery_outcomes.get(scene_num, [])
        recent_failures = [
            o for o in outcomes
            if o.get("details", {}).get("failure_class") == failure_class.value and not o.get("success", True)
        ]
        return len(recent_failures) >= 3

    def detect_thrashing(self, scene_num: int) -> RecoveryAction | None:
        outcomes = self._recovery_outcomes.get(scene_num, [])
        if len(outcomes) < 3:
            return None
        last_three = outcomes[-3:]
        actions = [o.get("action") for o in last_three]
        if len(set(actions)) == 1:
            successes = [o.get("success") for o in last_three]
            if not any(successes):
                return RecoveryAction.ESCALATE
        return None

    def record_recovery_outcome(
        self,
        scene_num: int,
        action: RecoveryAction,
        success: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        if scene_num not in self._recovery_outcomes:
            self._recovery_outcomes[scene_num] = []
        self._recovery_outcomes[scene_num].append({
            "action": action.value if hasattr(action, "value") else str(action),
            "success": success,
            "details": details or {},
            "timestamp": time.time(),
        })

    def _save_checkpoint(
        self,
        scene_num: int,
        reason: str,
    ) -> None:
        """Persist a checkpoint of current recovery state before escalation."""
        try:
            self._alert_dir.mkdir(parents=True, exist_ok=True)
            checkpoint: dict[str, Any] = {
                "schema_version": "1.0",
                "checkpoint_type": "RECOVERY_STATE",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scene_num": scene_num,
                "reason": reason,
                "run_level_attempts": {fc.value: cnt for fc, cnt in self._run_level_attempts.items()},
                "recovery_outcomes": {
                    str(sn): outs for sn, outs in self._recovery_outcomes.items()
                },
                "budgets": {
                    str(sn): repr(budget) for sn, budget in self._budgets.items()
                },
            }
            filename = f"recovery_checkpoint_scene{scene_num}_{int(time.time())}_{uuid.uuid4().hex[:8]}.json"
            checkpoint_path = self._alert_dir / filename
            checkpoint_path.write_text(json.dumps(checkpoint, indent=2, default=str), encoding="utf-8")
            logger.info(f"Recovery checkpoint saved to {checkpoint_path}")
        except Exception:
            logger.exception("Failed to write recovery checkpoint")

    def _write_escalation_alert(
        self,
        scene_num: int,
        failure_class: FailureClass,
        error_message: str,
        artifact_type: str,
        reason: str,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        """Persist a machine-readable alert when the pipeline stops for human intervention."""
        try:
            self._alert_dir.mkdir(parents=True, exist_ok=True)
            alert: dict[str, Any] = {
                "schema_version": "1.0",
                "alert_type": "RECOVERY_ESCALATION",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scene_num": scene_num,
                "artifact_type": artifact_type,
                "failure_class": failure_class.value,
                "error_message": error_message,
                "escalation_reason": reason,
                "run_level_attempts": {fc.value: cnt for fc, cnt in self._run_level_attempts.items()},
                "extra_context": extra_context,
                "recent_outcomes": self._recovery_outcomes.get(scene_num, [])[-5:],
            }
            filename = f"recovery_alert_scene{scene_num}_{int(time.time())}_{uuid.uuid4().hex[:8]}.json"
            alert_path = self._alert_dir / filename
            alert_path.write_text(json.dumps(alert, indent=2, default=str), encoding="utf-8")
            logger.info(f"Escalation alert written to {alert_path}")
        except Exception:
            logger.exception("Failed to write escalation alert file")

    def _build_escalation_reason(
        self,
        scene_num: int,
        failure_class: FailureClass,
        error_message: str,
        artifact_type: str,
        reason: str,
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        budget = self._budgets.get(scene_num)
        if budget is not None:
            try:
                budget_state = f"can_attempt({failure_class.value})={budget.can_attempt(failure_class)}"
            except Exception:
                budget_state = repr(budget)
        else:
            budget_state = "No budget found"

        outcomes = self._recovery_outcomes.get(scene_num, [])
        recent = outcomes[-5:] if outcomes else []
        history_lines: list[str] = []
        for idx, oc in enumerate(recent, 1):
            ts = oc.get("timestamp")
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if isinstance(ts, (int, float)) else "unknown"
            history_lines.append(
                f"    {idx}. [{ts_str}] action={oc.get('action')} success={oc.get('success')} "
                f"details={oc.get('details')}"
            )

        run_summary = {fc.value: cnt for fc, cnt in self._run_level_attempts.items()}

        lines = [
            "=" * 60,
            f"RECOVERY ESCALATION — {reason}",
            "=" * 60,
            f"Scene Number:       {scene_num}",
            f"Artifact Type:      {artifact_type}",
            f"Failure Class:      {failure_class.value}",
            f"Error Message:      {error_message}",
            f"Budget State:       {budget_state}",
            f"Run-level Attempts: {run_summary}",
            f"Scene History (last {len(recent)}):",
        ]
        if history_lines:
            lines.extend(history_lines)
        else:
            lines.append("    (no prior recovery outcomes recorded)")
        if extra_context:
            lines.append(f"Extra Context:      {extra_context}")
        lines.append(f"Timestamp:          {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        lines.append("=" * 60)

        return "\n".join(lines)