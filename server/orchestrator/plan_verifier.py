"""
Production plan verifier — structural validation (hard gate).

Inspired by mcp-agent's PlanVerifier, this validates that a ProductionPlan
is structurally correct before any GPU time is spent.  If verification
fails, the plan goes back to the optimizer for refinement.
"""

from __future__ import annotations

import logging
from typing import Optional

from orchestrator.production_models import (
    ClipTask,
    PlanVerificationResult,
    ProductionPlan,
)

logger = logging.getLogger(__name__)


class ProductionPlanVerifier:
    """Verifies production plans for structural correctness.

    This is a **hard gate** — if verification fails, no GPU time is spent.
    The plan is sent back to the optimizer with the error details.
    """

    def __init__(
        self,
        visual_concepts: list[dict],
        worker_urls: list[str],
        existing_clips: dict[str, str],
        known_lora_ids: Optional[set[str]] = None,
    ):
        """
        Args:
            visual_concepts: List of concept dicts from state["visual_concepts"].
            worker_urls: Available GPU worker URLs.
            existing_clips: Mapping of clip_id -> output_path for already-generated clips.
            known_lora_ids: Set of valid LoRA identifiers (optional).
        """
        self.visual_concepts = visual_concepts
        self.worker_urls = worker_urls
        self.existing_clips = existing_clips
        self.known_lora_ids = known_lora_ids or {
            "documentary-realism",
            "cinematic-noir",
            "retro-film",
            "nature-macro",
            "urban-grit",
            "watercolor-dream",
        }

        # Build expected clip IDs from visual concepts
        self.expected_clip_ids: set[str] = set()
        for concept in visual_concepts:
            sn = concept.get("scene_num", 0)
            pi = concept.get("phrase_idx", 0)
            self.expected_clip_ids.add(f"s{sn:03d}_p{pi:03d}")

    def verify(self, plan: ProductionPlan) -> PlanVerificationResult:
        """Run all verification checks and return the result."""
        result = PlanVerificationResult()

        self._check_completeness(plan, result)
        self._check_no_duplicates(plan, result)
        self._check_durations(plan, result)
        self._check_workers(plan, result)
        self._check_lora_ids(plan, result)
        self._check_resume_awareness(plan, result)
        self._check_batch_sizes(plan, result)
        self._check_clip_count(plan, result)

        if result.is_valid:
            logger.info("Plan verification PASSED (%d batches, %d total clips)",
                        len(plan.batches), self._total_clip_count(plan))
        else:
            logger.warning("Plan verification FAILED: %s", result.format_errors())

        return result

    # -- Individual checks --------------------------------------------------

    def _check_completeness(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 1: Every visual concept has a corresponding clip task."""
        planned_ids: set[str] = set()
        for batch in plan.batches:
            for task in batch.tasks:
                planned_ids.add(task.clip_id)

        missing = self.expected_clip_ids - planned_ids
        if missing:
            result.add_error(
                category="missing_clips",
                message=f"{len(missing)} visual concept(s) have no clip task: {sorted(missing)[:5]}",
                details={"missing_clip_ids": sorted(missing)},
            )

    def _check_no_duplicates(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 2: No duplicate clip_ids."""
        seen: dict[str, int] = {}
        for batch_idx, batch in enumerate(plan.batches):
            for task in batch.tasks:
                if task.clip_id in seen:
                    result.add_error(
                        category="duplicate_clip_id",
                        message=f"Clip '{task.clip_id}' appears in batch {seen[task.clip_id]} and batch {batch_idx}",
                        clip_id=task.clip_id,
                    )
                else:
                    seen[task.clip_id] = batch_idx

    def _check_durations(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 3: All durations ≤ 10.0 seconds."""
        for batch in plan.batches:
            for task in batch.tasks:
                if task.duration > 10.0:
                    result.add_error(
                        category="duration_violation",
                        message=f"Clip '{task.clip_id}' duration {task.duration}s exceeds 10.0s LTX-2.3 limit",
                        clip_id=task.clip_id,
                        details={"duration": task.duration, "limit": 10.0},
                    )

    def _check_workers(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 4: All assigned workers exist or are 'auto'."""
        valid_workers = set(self.worker_urls) | {"auto"}
        for batch in plan.batches:
            for task in batch.tasks:
                if task.assigned_worker not in valid_workers:
                    result.add_error(
                        category="invalid_worker",
                        message=f"Clip '{task.clip_id}' assigned to unknown worker '{task.assigned_worker}'",
                        clip_id=task.clip_id,
                        details={
                            "assigned_worker": task.assigned_worker,
                            "valid_workers": sorted(valid_workers),
                        },
                    )

    def _check_lora_ids(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 5: All LoRA IDs are from the known set (warning only)."""
        for batch in plan.batches:
            for task in batch.tasks:
                if task.lora_id not in self.known_lora_ids:
                    # Unknown LoRA is a warning, not an error — the visual
                    # director may have introduced new styles.
                    result.warnings.append(
                        f"Clip '{task.clip_id}' uses unknown LoRA '{task.lora_id}' "
                        f"(known: {sorted(self.known_lora_ids)})"
                    )

    def _check_resume_awareness(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 6: Already-generated clips with good QA are not re-planned for generation."""
        for batch in plan.batches:
            for task in batch.tasks:
                if task.clip_id in self.existing_clips and not task.skip:
                    result.add_error(
                        category="resume_violation",
                        message=(
                            f"Clip '{task.clip_id}' already exists with good QA "
                            f"but is planned for regeneration (skip=false)"
                        ),
                        clip_id=task.clip_id,
                    )

    def _check_batch_sizes(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 7: Batch sizes don't exceed worker count."""
        num_workers = max(len(self.worker_urls), 1)
        for batch_idx, batch in enumerate(plan.batches):
            active_tasks = [t for t in batch.tasks if not t.skip]
            if len(active_tasks) > num_workers:
                result.warnings.append(
                    f"Batch {batch_idx} has {len(active_tasks)} active tasks "
                    f"but only {num_workers} worker(s) — some clips will queue"
                )

    def _check_clip_count(self, plan: ProductionPlan, result: PlanVerificationResult) -> None:
        """Check 8: Total clip count matches visual_concepts count."""
        planned_ids: set[str] = set()
        for batch in plan.batches:
            for task in batch.tasks:
                planned_ids.add(task.clip_id)

        extra = planned_ids - self.expected_clip_ids
        if extra:
            result.warnings.append(
                f"{len(extra)} unexpected clip(s) in plan: {sorted(extra)[:5]}"
            )

        if len(planned_ids) != len(self.expected_clip_ids):
            # Completeness check already catches missing, this catches count mismatch
            if len(planned_ids) < len(self.expected_clip_ids):
                result.add_error(
                    category="clip_count_mismatch",
                    message=(
                        f"Plan has {len(planned_ids)} clips but {len(self.expected_clip_ids)} expected"
                    ),
                    details={
                        "planned": len(planned_ids),
                        "expected": len(self.expected_clip_ids),
                    },
                )

    # -- Helpers ------------------------------------------------------------

    def _total_clip_count(self, plan: ProductionPlan) -> int:
        return sum(len(batch.tasks) for batch in plan.batches)
