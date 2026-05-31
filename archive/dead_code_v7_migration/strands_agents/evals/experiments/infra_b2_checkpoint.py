"""B2 checkpoint helper experiment (slice 6).

Drives :mod:`strands_agents.b2_checkpoint` through scripted operation
sequences against an :class:`InMemoryB2CheckpointStore` and scores the
outcomes through deterministic :class:`strands_evals.Evaluator`
subclasses.

The checkpoint helper is pure Python with no external service on this
path, so this experiment is offline — no real B2 traffic, no network.
Each Case runs a short scripted sequence of operations (``upload``,
``list_for_run``, ``resume``, and one test-only ``corrupt`` hook) and
scores the *final* operation's outcome against expectation.

Cases cover:

* **happy_path_upload** — a fresh run_id + one scene_json upload
  returns an entry whose fields match expectation.
* **duplicate_idempotency** — two uploads of the same bytes/revision
  return the same ``artifact_id`` and the ledger holds one entry.
* **stale_revision_rejected** — upload r0002 then try r0001 → raises
  :class:`StaleRevisionError`. Invariant 8: revision tags are sacred.
* **round_trip_manifest** — upload N artifacts, ``list_for_run``
  returns them in upload order with all fields intact.
* **resume_happy_path** — upload a few artifacts → ``resume`` →
  :attr:`ResumeState.latest_revision_tag` matches the last upload
  and no kinds are missing for the kinds we uploaded.
* **resume_checksum_mismatch_fails_closed** — corrupt an artifact's
  bytes → ``resume`` raises :class:`ChecksumMismatchError` and no
  partial state escapes.
* **missing_manifest_raises** — ``load_manifest`` on an unknown
  run_id raises :class:`ManifestMissingError` (NOT an empty manifest
  — the two states must be distinguishable downstream).
* **out_of_order_insert_preserves_upload_order** — uploads are
  appended in the order they complete; ``list_for_run`` reproduces
  that order even if wall-clock timestamps are close.

Every Case scores two evaluators:

* :class:`B2OutcomeEvaluator` — the final operation's outcome string
  matches ``metadata.expected_outcome`` (``success`` or
  ``raises/<ExceptionName>``).
* :class:`B2DetailEvaluator` — structural check on the outcome
  payload (entry fields, revision ordering, missing-kinds list,
  error attributes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

from strands_agents.b2_checkpoint import (
    ARTIFACT_KINDS,
    ChecksumMismatchError,
    DuplicateIdempotencyKeyError,
    InMemoryB2CheckpointStore,
    Manifest,
    ManifestEntry,
    ManifestMissingError,
    ResumeState,
    StaleRevisionError,
    load_manifest,
    resume,
)


#: Thresholds advertised to the playground catalog. Both evaluators
#: are hard gates — a checkpoint store that picks the wrong outcome
#: or reports the wrong detail is a data-loss regression.
INFRA_B2_CHECKPOINT_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "B2OutcomeEvaluator": (1.0, True),
    "B2DetailEvaluator": (1.0, True),
}


# ── Scripted-scenario schema ─────────────────────────────────────────
#
# Each case's ``input`` is ``{"operations": [...]}`` — a list of
# ``{"op": <name>, ...args}`` dicts applied in order against a fresh
# :class:`InMemoryB2CheckpointStore`. The *final* operation's outcome
# is scored.
#
# Supported operations:
#   * ``upload`` — call :func:`checkpoint_artifact` via an in-memory
#     file. Args: ``run_id``, ``kind``, ``revision_tag``,
#     ``payload_text`` (utf-8 bytes).
#   * ``list_for_run`` — :func:`load_manifest` for the given run_id.
#   * ``resume`` — :func:`resume` for the given run_id.
#   * ``corrupt`` — test-only hook: swap the bytes behind a previously
#     uploaded ``artifact_alias`` (the string the test used in the
#     ``upload`` op) without touching the manifest entry.


def _case(
    name: str,
    *,
    operations: list[dict[str, Any]],
    expected_outcome: str,
    expected_detail: dict[str, Any] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-b2-checkpoint-{name}",
        input={"operations": operations},
        expected_output={"outcome": expected_outcome},
        metadata={
            "expected_outcome": expected_outcome,
            "expected_detail": expected_detail or {},
        },
    )


def _upload(
    *,
    run_id: str,
    kind: str,
    revision_tag: str,
    payload_text: str,
    alias: str | None = None,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "upload",
        "run_id": run_id,
        "kind": kind,
        "revision_tag": revision_tag,
        "payload_text": payload_text,
    }
    if alias is not None:
        op["alias"] = alias
    return op


def infra_b2_checkpoint_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical invariant-enforcement suite."""
    return [
        _case(
            "happy_path_upload",
            operations=[
                _upload(
                    run_id="run-a",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text='{"scene":1}',
                ),
            ],
            expected_outcome="success",
            expected_detail={
                "kind": "scene_json",
                "revision_tag": "r0001",
                "run_id": "run-a",
                "size_bytes": 11,
            },
        ),
        _case(
            "duplicate_idempotency",
            operations=[
                _upload(
                    run_id="run-b",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="hello",
                    alias="first",
                ),
                _upload(
                    run_id="run-b",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="hello",
                    alias="second",
                ),
            ],
            expected_outcome="success",
            expected_detail={
                "manifest_entry_count": 1,
                "artifact_id_stable_across_retry": True,
            },
        ),
        _case(
            "stale_revision_rejected",
            operations=[
                _upload(
                    run_id="run-c",
                    kind="scene_json",
                    revision_tag="r0002",
                    payload_text="second-rev",
                ),
                _upload(
                    run_id="run-c",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="first-rev",
                ),
            ],
            expected_outcome="raises/StaleRevisionError",
            expected_detail={
                "attempted_revision_tag": "r0001",
                "latest_revision_tag": "r0002",
            },
        ),
        _case(
            "round_trip_manifest",
            operations=[
                _upload(
                    run_id="run-d",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="s1",
                ),
                _upload(
                    run_id="run-d",
                    kind="audio_wav",
                    revision_tag="r0001",
                    payload_text="audio-bytes",
                ),
                _upload(
                    run_id="run-d",
                    kind="video_mp4",
                    revision_tag="r0002",
                    payload_text="video-bytes",
                ),
                {"op": "list_for_run", "run_id": "run-d"},
            ],
            expected_outcome="success",
            expected_detail={
                "manifest_entry_count": 3,
                "manifest_kinds_in_order": [
                    "scene_json",
                    "audio_wav",
                    "video_mp4",
                ],
                "manifest_latest_revision_tag": "r0002",
            },
        ),
        _case(
            "resume_happy_path",
            operations=[
                _upload(
                    run_id="run-e",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="s1",
                ),
                _upload(
                    run_id="run-e",
                    kind="audio_wav",
                    revision_tag="r0001",
                    payload_text="a1",
                ),
                {"op": "resume", "run_id": "run-e"},
            ],
            expected_outcome="success",
            expected_detail={
                "resume_latest_revision_tag": "r0001",
                "resume_missing_kinds_excludes": [
                    "scene_json",
                    "audio_wav",
                ],
                "resume_missing_kinds_includes": [
                    "video_mp4",
                    "timing_alignment",
                    "otio_xml",
                    "master_mp4",
                ],
            },
        ),
        _case(
            "resume_checksum_mismatch_fails_closed",
            operations=[
                _upload(
                    run_id="run-f",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="original",
                    alias="victim",
                ),
                {
                    "op": "corrupt",
                    "alias": "victim",
                    "new_payload_text": "tampered",
                },
                {"op": "resume", "run_id": "run-f"},
            ],
            expected_outcome="raises/ChecksumMismatchError",
            expected_detail={
                "error_has_artifact_id": True,
            },
        ),
        _case(
            "missing_manifest_raises",
            operations=[
                {"op": "list_for_run", "run_id": "run-ghost"},
            ],
            expected_outcome="raises/ManifestMissingError",
            expected_detail={
                "error_run_id": "run-ghost",
            },
        ),
        _case(
            "out_of_order_insert_preserves_upload_order",
            operations=[
                _upload(
                    run_id="run-g",
                    kind="audio_wav",
                    revision_tag="r0001",
                    payload_text="a-first",
                ),
                _upload(
                    run_id="run-g",
                    kind="scene_json",
                    revision_tag="r0001",
                    payload_text="s-second",
                ),
                _upload(
                    run_id="run-g",
                    kind="video_mp4",
                    revision_tag="r0001",
                    payload_text="v-third",
                ),
                {"op": "list_for_run", "run_id": "run-g"},
            ],
            expected_outcome="success",
            expected_detail={
                "manifest_kinds_in_order": [
                    "audio_wav",
                    "scene_json",
                    "video_mp4",
                ],
            },
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


@dataclass
class _OpResult:
    """One operation's outcome inside the task's history."""

    op: str
    outcome: str
    detail: dict[str, Any] = field(default_factory=dict)


def _apply_operation(
    *,
    store: InMemoryB2CheckpointStore,
    op: dict[str, Any],
    aliases: dict[str, str],
    last_entry_ref: list[ManifestEntry | None],
) -> _OpResult:
    name = op["op"]
    try:
        if name == "upload":
            payload = op["payload_text"].encode("utf-8")
            # Use the store's ``upload`` directly to avoid a temp file
            # — ``checkpoint_artifact`` is a thin wrapper around it and
            # the in-memory variant only cares about bytes.
            entry = store.upload(
                payload=payload,
                kind=op["kind"],
                revision_tag=op["revision_tag"],
                run_id=op["run_id"],
            )
            last_entry_ref[0] = entry
            alias = op.get("alias")
            if alias is not None:
                aliases[alias] = entry.artifact_id
            return _OpResult(
                op="upload",
                outcome="success",
                detail={
                    "artifact_id": entry.artifact_id,
                    "run_id": entry.run_id,
                    "kind": entry.kind,
                    "revision_tag": entry.revision_tag,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                    "idempotency_key": entry.idempotency_key,
                },
            )
        if name == "list_for_run":
            manifest = load_manifest(run_id=op["run_id"], store=store)
            return _OpResult(
                op="list_for_run",
                outcome="success",
                detail={
                    "manifest_entry_count": len(manifest.entries),
                    "manifest_kinds_in_order": [
                        e.kind for e in manifest.entries
                    ],
                    "manifest_latest_revision_tag": (
                        manifest.latest_revision_tag
                    ),
                    "manifest_run_id": manifest.run_id,
                },
            )
        if name == "resume":
            state: ResumeState = resume(run_id=op["run_id"], store=store)
            return _OpResult(
                op="resume",
                outcome="success",
                detail={
                    "resume_run_id": state.run_id,
                    "resume_latest_revision_tag": state.latest_revision_tag,
                    "resume_missing_kinds": list(state.missing_kinds),
                    "resume_artifact_count_by_kind": {
                        kind: len(state.artifacts_by_kind[kind])
                        for kind in ARTIFACT_KINDS
                    },
                    "resume_stale_revision_entry_count": (
                        len(state.stale_revision_entries)
                    ),
                },
            )
        if name == "corrupt":
            alias = op["alias"]
            artifact_id = aliases[alias]
            new_bytes = op["new_payload_text"].encode("utf-8")
            store._corrupt_for_testing(
                artifact_id=artifact_id, new_bytes=new_bytes
            )
            return _OpResult(
                op="corrupt",
                outcome="success",
                detail={"artifact_id": artifact_id},
            )
        return _OpResult(
            op=name,
            outcome="raises/UnknownOperation",
            detail={"op": name},
        )
    except StaleRevisionError as err:
        return _OpResult(
            op=name,
            outcome="raises/StaleRevisionError",
            detail={
                "run_id": err.run_id,
                "attempted_revision_tag": err.attempted_revision_tag,
                "latest_revision_tag": err.latest_revision_tag,
            },
        )
    except DuplicateIdempotencyKeyError as err:
        return _OpResult(
            op=name,
            outcome="raises/DuplicateIdempotencyKeyError",
            detail={
                "idempotency_key": err.idempotency_key,
                "existing_artifact_id": err.existing_artifact_id,
                "incoming_sha256": err.incoming_sha256,
            },
        )
    except ChecksumMismatchError as err:
        return _OpResult(
            op=name,
            outcome="raises/ChecksumMismatchError",
            detail={
                "artifact_id": err.artifact_id,
                "expected_sha256": err.expected_sha256,
                "actual_sha256": err.actual_sha256,
            },
        )
    except ManifestMissingError as err:
        return _OpResult(
            op=name,
            outcome="raises/ManifestMissingError",
            detail={"run_id": err.run_id},
        )


def infra_b2_checkpoint_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the case's operation sequence against a fresh store.

    Returns the evaluate-friendly envelope: the *final* operation's
    ``outcome`` and ``detail`` under ``output``; earlier operations
    accumulate in ``metadata.history``.
    """
    payload = case.input or {}
    operations: list[dict[str, Any]] = payload.get("operations", [])

    store = InMemoryB2CheckpointStore()
    aliases: dict[str, str] = {}
    last_entry_ref: list[ManifestEntry | None] = [None]

    history: list[dict[str, Any]] = []
    trajectory: list[str] = []
    final = _OpResult(op="noop", outcome="success")
    # Hold on to the first successful upload's artifact_id to enable
    # the ``artifact_id_stable_across_retry`` detail check for the
    # idempotency case.
    first_upload_artifact_id: str | None = None
    for op in operations:
        result = _apply_operation(
            store=store,
            op=op,
            aliases=aliases,
            last_entry_ref=last_entry_ref,
        )
        trajectory.append(result.op)
        history.append(
            {"op": result.op, "outcome": result.outcome, "detail": result.detail}
        )
        if (
            result.op == "upload"
            and result.outcome == "success"
            and first_upload_artifact_id is None
        ):
            first_upload_artifact_id = str(result.detail.get("artifact_id"))
        final = result

    # Derive a couple of extra details on the final outcome so the
    # evaluator can make structural assertions without re-running the
    # whole sequence.
    enriched_detail = dict(final.detail)
    enriched_detail.setdefault(
        "artifact_id_stable_across_retry",
        _all_upload_artifact_ids_equal(history),
    )
    try:
        manifest_for_run = _last_seen_run_id_manifest(store, operations)
    except ManifestMissingError:
        manifest_for_run = None
    if manifest_for_run is not None:
        enriched_detail.setdefault(
            "manifest_entry_count", len(manifest_for_run.entries)
        )
        enriched_detail.setdefault(
            "manifest_kinds_in_order",
            [e.kind for e in manifest_for_run.entries],
        )
        enriched_detail.setdefault(
            "manifest_latest_revision_tag",
            manifest_for_run.latest_revision_tag,
        )

    return {
        "output": {
            "outcome": final.outcome,
            "detail": enriched_detail,
        },
        "trajectory": trajectory,
        "metadata": {
            "history": history,
            "operations_applied": len(operations),
            "first_upload_artifact_id": first_upload_artifact_id,
        },
    }


def _all_upload_artifact_ids_equal(history: list[dict[str, Any]]) -> bool:
    """Return True iff every successful ``upload`` returned the same id.

    Used only for the idempotency Case's ``artifact_id_stable_across_retry``
    detail. The evaluator pins this boolean against expectation.
    """
    ids: list[str] = []
    for row in history:
        if row["op"] == "upload" and row["outcome"] == "success":
            ids.append(str(row["detail"]["artifact_id"]))
    if len(ids) <= 1:
        return True
    return all(aid == ids[0] for aid in ids)


def _last_seen_run_id_manifest(
    store: InMemoryB2CheckpointStore, operations: list[dict[str, Any]]
) -> Manifest | None:
    """Return the manifest for the run_id the case most recently touched."""
    run_id: str | None = None
    for op in operations:
        run_id = op.get("run_id", run_id)
    if run_id is None:
        return None
    try:
        return store.list_for_run(run_id)
    except ManifestMissingError:
        return None


# ── Evaluators ───────────────────────────────────────────────────────


class B2OutcomeEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the final operation's ``outcome`` to the expected string.

    Success here means "the helper correctly accepted or correctly
    rejected the sequence" — the sign of the outcome matters.
    ``raises/<ExceptionName>`` outcomes are as valid as ``success``
    when the Case expects a rejection.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = (evaluation_case.actual_output or {}).get("outcome")
        expected = (evaluation_case.metadata or {}).get("expected_outcome")
        match = actual == expected
        return [
            EvaluationOutput(
                score=1.0 if match else 0.0,
                test_pass=match,
                reason=(
                    f"outcome={actual!r} "
                    f"{'matches' if match else 'does not match'} "
                    f"expected={expected!r}"
                ),
                label="outcome_match" if match else "outcome_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class B2DetailEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Structural check on the final outcome's detail payload.

    Keys in ``expected_detail`` are matched against the actual detail
    with per-key semantics:

    * ``manifest_kinds_in_order`` / ``resume_missing_kinds_excludes``
      compare lists.
    * ``resume_missing_kinds_includes`` checks that every element in
      the expected list is present in the actual list (the actual may
      include more).
    * ``resume_missing_kinds_excludes`` checks that no expected
      element appears in the actual list.
    * ``error_has_artifact_id`` checks that the actual detail has a
      non-empty ``artifact_id`` field.
    * Every other key is compared with ``==``.

    Cases without ``expected_detail`` pass the evaluator trivially.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        expected = dict(metadata.get("expected_detail") or {})
        actual = dict(
            (evaluation_case.actual_output or {}).get("detail") or {}
        )

        if not expected:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no detail requested for this case",
                    label="detail_not_required",
                )
            ]

        mismatches: list[str] = []
        for key, expected_value in expected.items():
            if key == "resume_missing_kinds_excludes":
                actual_missing = list(actual.get("resume_missing_kinds") or [])
                leaked = [k for k in expected_value if k in actual_missing]
                if leaked:
                    mismatches.append(
                        f"{key}: kinds {leaked!r} should NOT be in "
                        f"resume_missing_kinds={actual_missing!r}"
                    )
                continue
            if key == "resume_missing_kinds_includes":
                actual_missing = list(actual.get("resume_missing_kinds") or [])
                absent = [k for k in expected_value if k not in actual_missing]
                if absent:
                    mismatches.append(
                        f"{key}: kinds {absent!r} expected in "
                        f"resume_missing_kinds={actual_missing!r}"
                    )
                continue
            if key == "error_has_artifact_id":
                actual_artifact_id = actual.get("artifact_id")
                if bool(actual_artifact_id) != bool(expected_value):
                    mismatches.append(
                        f"{key}: expected has_artifact_id={bool(expected_value)} "
                        f"but actual artifact_id={actual_artifact_id!r}"
                    )
                continue
            if key == "error_run_id":
                actual_run_id = actual.get("run_id")
                if actual_run_id != expected_value:
                    mismatches.append(
                        f"{key}: actual run_id={actual_run_id!r} "
                        f"expected {expected_value!r}"
                    )
                continue
            actual_value = actual.get(key)
            if actual_value != expected_value:
                mismatches.append(
                    f"{key}={actual_value!r} expected {expected_value!r}"
                )

        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "all detail fields match expected"
                    if ok
                    else "detail mismatches: " + "; ".join(mismatches)
                ),
                label="detail_match" if ok else "detail_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_b2_checkpoint_experiment() -> (
    Experiment[dict[str, Any], dict[str, Any]]
):
    """Assemble the B2 checkpoint :class:`Experiment`.

    Returns:
        A fully wired experiment covering the eight canonical Cases
        and the two deterministic evaluators. Ready for
        :meth:`Experiment.run_evaluations` or to be surfaced through
        the playground's ``/playground/components/{id}/evaluate``
        endpoint.
    """
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_b2_checkpoint_cases(),
        evaluators=[
            B2OutcomeEvaluator(),
            B2DetailEvaluator(),
        ],
    )


__all__ = ["B2DetailEvaluator",
    "B2OutcomeEvaluator",
    "build_infra_b2_checkpoint_experiment",
    "infra_b2_checkpoint_cases",
    "infra_b2_checkpoint_task",]
