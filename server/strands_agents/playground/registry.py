"""Declarative registry for the 15 atomic components.

Each entry binds one component id (``c01``-``c15``) to:

* Human-facing metadata (title, kind, atlas row).
* The ``*_cases()`` factory from
  ``server/strands_agents/evals/experiments`` so the case catalog is
  always in lockstep with the CI corpus.
* The component's evaluator stack and per-evaluator thresholds, read
  directly from each experiment module's ``*_THRESHOLDS`` dict — the
  same source of truth used by ``eval-framework/THRESHOLDS.md`` and by
  the CI runner.
* The canonical + candidate models the component is declared to run
  against. Models live here, not scattered across agent modules, so the
  playground and future ratchet tools can answer "which models is this
  component qualified against today?" in one lookup.

The registry is intentionally data-only. Running a component, probing
its model, or writing a case is handled by other playground modules.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from strands_evals.case import Case


#: Well-known pipeline-component identifiers. Ordering mirrors the
#: test-case atlas rows.
COMPONENT_IDS: tuple[str, ...] = (
    # Row 1
    "c01",
    "c02",
    "c03",
    "c04",
    "c05",
    # Row 2
    "c06",
    "c07",
    "c08",
    "c09",
    "c10",
    # Row 3
    "c11",
    "c12",
    "c13",
    "c14",
    "c15",
)

#: Infrastructure-unit identifiers — the worker VMs, the cost
#: guardian, the worker registry, the per-VM infra agent. Surfaced
#: in the same playground as the pipeline components so every unit
#: the orchestrator depends on is user-auditable from the UI. These
#: are NOT members of :data:`COMPONENT_IDS` because they do not
#: occupy an atlas row — they sit beneath the pipeline rather than
#: alongside it.
INFRA_COMPONENT_IDS: tuple[str, ...] = (
    "infra_guardian",
    "infra_worker_registry",
    "infra_agent",
    "infra_qwen3_tts_worker",
    "infra_ltx_video_worker",
    "infra_ltx_video_worker_live",
    "infra_b2_checkpoint",
    "infra_pipeline_adapter",
    "infra_pipeline_live_orchestrator",
    "qa_video_artifact_probe",
    "qa_duration_align",
    "qa_stills_judge",
)


@dataclass(frozen=True)
class DeclaredModel:
    """One model a component is declared to run against.

    Attributes:
        id: The ``strands.Agent(model=...)`` string (e.g.
            ``openai/gpt-4o``). The playground never substitutes a
            model outside the declared set.
        provider: Short provider label for the UI (``openai``,
            ``gemini``, ``local``, …).
        role: Why this model is on the list — ``canonical`` (the one CI
            runs) or ``candidate`` (also qualified; useful for
            side-by-side compare in the workbench).
    """

    id: str
    provider: str
    role: str  # "canonical" | "candidate"


@dataclass(frozen=True)
class EvaluatorDeclaration:
    """One evaluator in a component's stack.

    Attributes:
        name: The ``Evaluator`` subclass name, as it appears in the
            component's ``*_EVALUATOR_THRESHOLDS`` dict.
        threshold: The minimum ``overall_score`` the CI runner accepts
            before flagging a regression.
        hard_gate: Whether a single-case regression fails the build.
    """

    name: str
    threshold: float
    hard_gate: bool


@dataclass(frozen=True)
class Component:
    """Declarative record for one atomic component.

    The registry hands these out in insertion order; callers that need
    a specific one look it up by ``id``.
    """

    id: str
    title: str
    kind: str  # "leaf" | "tool" | "loop" | "graph" | "gate"
    row: int
    summary: str
    experiment_module: str
    cases_factory: str
    thresholds_attr: str
    declared_models: tuple[DeclaredModel, ...]
    #: Name of the ``*_task`` callable exported by the experiment
    #: module, if any. The playground's run endpoint dispatches to it
    #: after the model-reachability probe passes. Components whose
    #: upstream experiment does not yet export a task callable
    #: surface as ``NO_TASK_ADAPTER`` at run time — a deliberate
    #: visible gap rather than a silent fallback.
    task_attr: str | None = None
    #: Name of the ``build_*_experiment`` callable that returns the
    #: canonical :class:`Experiment` for this component. The
    #: playground's evaluate endpoint instantiates it once and reuses
    #: the ``.evaluators`` list to score user-supplied outputs. Kept
    #: ``None`` for components whose builder is still pending.
    experiment_builder_attr: str | None = None

    # Internal caches populated on first access. The dataclass is
    # frozen, so we stash these in a mutable default via ``field``.
    _cache: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    def cases(self) -> list[Case[Any, Any]]:
        """Return the component's canonical cases.

        The factory is imported lazily so the playground can be mounted
        even when some components haven't been finished — a partially
        missing import surfaces as an empty case list rather than a
        hard boot failure.

        ``cases_factory`` can either name a callable returning
        ``list[Case]`` (e.g. ``scenario_cases``) or a module-level list
        attribute (e.g. ``_CASES``) — matches the two shapes already in
        ``server/strands_agents/evals/experiments``.
        """
        cached = self._cache.get("cases")
        if cached is not None:
            return cached

        try:
            module = importlib.import_module(self.experiment_module)
            source: Any = getattr(module, self.cases_factory)
            cases = source() if callable(source) else list(source)
        except Exception:  # noqa: BLE001 — partial rollout safety net
            cases = []
        cases = [self._normalised_case(c, i) for i, c in enumerate(cases)]
        self._cache["cases"] = cases
        return cases

    @staticmethod
    def _normalised_case(case: Case[Any, Any], index: int) -> Case[Any, Any]:
        """Promote ``metadata["case_name"]`` to ``Case.name`` when missing.

        Some upstream experiments (notably c04 audio) historically set
        the case name in metadata rather than on the dataclass. Those
        cases become unaddressable by the run/evaluate endpoints, since
        the endpoints key lookups by ``Case.name``. Normalising once
        at registry surface keeps endpoint code uniform without
        touching the source experiments.
        """
        if getattr(case, "name", None):
            return case
        metadata = getattr(case, "metadata", None) or {}
        promoted = metadata.get("case_name") or f"case_{index}"
        try:
            return dataclasses.replace(case, name=promoted)
        except (dataclasses.FrozenInstanceError, TypeError):
            # Fall back to attribute assignment for non-dataclass Case
            # shapes; the playground's catalog serialiser reads .name.
            try:
                case.name = promoted  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            return case

    def evaluators(self) -> list[EvaluatorDeclaration]:
        """Return the component's evaluator stack in declaration order.

        Components without a ``*_EVALUATOR_THRESHOLDS`` dict today
        (the orchestration-level graph, approval gates, recovery,
        escalation, assembly) return an empty list. Later PRs wire
        their thresholds in; the catalog surfaces the gap so it's
        visible.
        """
        cached = self._cache.get("evaluators")
        if cached is not None:
            return cached

        try:
            module = importlib.import_module(self.experiment_module)
            thresholds: dict[str, tuple[float, bool]] = getattr(
                module, self.thresholds_attr
            )
            decls = [
                EvaluatorDeclaration(name=name, threshold=score, hard_gate=hard)
                for name, (score, hard) in thresholds.items()
            ]
        except Exception:  # noqa: BLE001
            decls = []
        self._cache["evaluators"] = decls
        return decls

    def task(self) -> Callable[[Case[Any, Any]], Any] | None:
        """Return the component's task callable, lazy-loaded.

        ``None`` when the component has no ``task_attr`` declared or
        the upstream experiment module fails to expose it. Callers
        surface a ``NO_TASK_ADAPTER`` status to the frontend rather
        than crashing.
        """
        if self.task_attr is None:
            return None
        cached = self._cache.get("task")
        if cached is not None:
            return cached if cached is not _MISSING_TASK else None

        try:
            module = importlib.import_module(self.experiment_module)
            callable_obj = getattr(module, self.task_attr, None)
            if callable_obj is None or not callable(callable_obj):
                self._cache["task"] = _MISSING_TASK
                return None
        except Exception:  # noqa: BLE001 — partial rollout safety net
            self._cache["task"] = _MISSING_TASK
            return None
        self._cache["task"] = callable_obj
        return callable_obj

    def evaluator_instances(self) -> list[Any]:
        """Return live :class:`Evaluator` instances from the builder.

        Empty list when the builder is missing, fails to import, or
        raises at construction time. Callers surface a
        ``NO_EVALUATORS`` status rather than 500-ing. Cached, so the
        builder runs at most once per component per process.
        """
        if self.experiment_builder_attr is None:
            return []
        cached = self._cache.get("evaluator_instances")
        if cached is not None:
            return cached if cached is not _MISSING_BUILDER else []

        try:
            module = importlib.import_module(self.experiment_module)
            builder = getattr(module, self.experiment_builder_attr, None)
            if builder is None or not callable(builder):
                self._cache["evaluator_instances"] = _MISSING_BUILDER
                return []
            experiment = builder()
            instances = list(experiment.evaluators)
        except Exception:  # noqa: BLE001 — partial rollout safety net
            self._cache["evaluator_instances"] = _MISSING_BUILDER
            return []
        self._cache["evaluator_instances"] = instances
        return instances


#: Internal sentinel so the ``task()`` cache can distinguish "never
#: checked" from "checked, not available". ``None`` would collide with
#: the public "no task adapter" semantic.
_MISSING_TASK = object()

#: Same idea for the evaluator-builder cache — ``[]`` is a valid
#: cached value (component declares no evaluators) so a distinct
#: sentinel is needed for "builder missing / failed".
_MISSING_BUILDER = object()


#: LiteLLM exposes the current Gemini 3 Pro as
#: ``gemini/gemini-3-pro-preview`` (verified against the live Gemini API
#: on the staging VM with ``GOOGLE_API_KEY``). The older
#: ``gemini-3.1-pro`` string was not a real model id and returned
#: ``404 NOT_FOUND`` from ``generativelanguage.googleapis.com``. See
#: ``docs/strands-migration/deploy/PLAYGROUND_STAGING.md`` for the
#: verification command.
_GEMINI_3_PRO: DeclaredModel = DeclaredModel(
    id="gemini/gemini-3-pro-preview", provider="gemini", role="canonical"
)
_GEMMA_4_URC: DeclaredModel = DeclaredModel(
    id="local/gemma-4-uncensored", provider="local", role="candidate"
)
_QWEN_OMNI: DeclaredModel = DeclaredModel(
    id="local/qwen3.5-omni", provider="local", role="candidate"
)
_OPENAI_4O: DeclaredModel = DeclaredModel(
    id="openai/gpt-4o", provider="openai", role="canonical"
)
_KIMI_K2: DeclaredModel = DeclaredModel(
    id="moonshot/kimi-k2", provider="moonshot", role="candidate"
)


_COMPONENTS: tuple[Component, ...] = (
    Component(
        id="c01",
        title="Scenario",
        kind="leaf",
        row=1,
        summary=(
            "Generates the scene list + style_lock and iterates "
            "generate → evaluate → refine until the structural checks "
            "return GOOD or EXCELLENT."
        ),
        experiment_module="strands_agents.evals.experiments.scenario",
        cases_factory="scenario_cases",
        thresholds_attr="SCENARIO_EVALUATOR_THRESHOLDS",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O, _KIMI_K2),
        experiment_builder_attr="build_scenario_experiment",
        task_attr="scenario_task",
    ),
    Component(
        id="c02",
        title="Timing Evaluator",
        kind="tool",
        row=1,
        summary=(
            "Pure @tool. Compares the WhisperX alignment against the "
            "scene targets under the intent or legacy tolerance path "
            "and returns timing_passed + per-scene report."
        ),
        experiment_module="strands_agents.evals.experiments.timing",
        cases_factory="timing_cases",
        thresholds_attr="TIMING_EVALUATOR_THRESHOLDS",
        declared_models=(),  # deterministic tool: no LLM
        experiment_builder_attr="build_experiment",
        task_attr="timing_task",
    ),
    Component(
        id="c03",
        title="Scenario Refiner",
        kind="leaf",
        row=1,
        summary=(
            "Adjusts scene durations / pronunciation hints based on "
            "the timing report. No-ops when timing already passes."
        ),
        experiment_module="strands_agents.evals.experiments.scenario_refiner",
        cases_factory="refiner_cases",
        thresholds_attr="SCENARIO_REFINER_EVALUATOR_THRESHOLDS",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        experiment_builder_attr="build_refiner_experiment",
        task_attr="scenario_refiner_task",
    ),
    Component(
        id="c04",
        title="Audio Agent",
        kind="tool",
        row=1,
        summary=(
            "Pure @tool. Dispatches TTS per voice block, runs WhisperX "
            "alignment, writes into the OTIO timeline, and verifies "
            "all seven audio invariants."
        ),
        experiment_module="strands_agents.evals.experiments.audio",
        cases_factory="audio_cases",
        thresholds_attr="AUDIO_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_audio_experiment",
        declared_models=(),  # TTS + WhisperX, not an LLM agent
        task_attr="audio_task",
    ),
    Component(
        id="c05",
        title="Timing Loop",
        kind="loop",
        row=1,
        summary=(
            "Cyclic GraphBuilder: audio → timing → refiner, max 10 "
            "iterations. Escalates on refiner no-op."
        ),
        experiment_module="strands_agents.evals.experiments.timing_loop",
        cases_factory="timing_loop_cases",
        thresholds_attr="TIMING_LOOP_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_timing_loop_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        task_attr="timing_loop_task",
    ),
    Component(
        id="c06",
        title="Content Analyst",
        kind="leaf",
        row=2,
        summary=(
            "Reads the finished narration + timeline and produces the "
            "per-scene content_analysis the visual concepter consumes."
        ),
        experiment_module="strands_agents.evals.experiments.content_analyst",
        cases_factory="content_analyst_cases",
        thresholds_attr="CONTENT_ANALYST_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_content_analyst_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        task_attr="content_analyst_task",
    ),
    Component(
        id="c07",
        title="Visual Concepter",
        kind="leaf",
        row=2,
        summary=(
            "Emits a visual concept per scene, honouring the "
            "style_lock. Picks the tool that matches the phrase type."
        ),
        experiment_module="strands_agents.evals.experiments.visual_concepter",
        cases_factory="visual_concepter_cases",
        thresholds_attr="VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_visual_concepter_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        task_attr="visual_concepter_task",
    ),
    Component(
        id="c08",
        title="Coherence Evaluator",
        kind="leaf",
        row=2,
        summary=(
            "LLM-as-judge. Scores visual concepts for coherence, "
            "style-lock adherence, and camera variety across scenes."
        ),
        experiment_module="strands_agents.evals.experiments.coherence_evaluator",
        cases_factory="coherence_evaluator_cases",
        thresholds_attr="COHERENCE_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_coherence_evaluator_experiment",
        declared_models=(_GEMINI_3_PRO, _QWEN_OMNI, _GEMMA_4_URC),
        task_attr="coherence_evaluator_task",
    ),
    Component(
        id="c09",
        title="Visual Loop",
        kind="loop",
        row=2,
        summary=(
            "Cyclic GraphBuilder: analyst → concepter → coherence, "
            "max 5 iterations. Stops on GOOD or EXCELLENT."
        ),
        experiment_module="strands_agents.evals.experiments.visual_loop",
        cases_factory="visual_loop_cases",
        thresholds_attr="VISUAL_LOOP_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_visual_loop_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        task_attr="visual_loop_task",
    ),
    Component(
        id="c10",
        title="Production Supervisor",
        kind="leaf",
        row=2,
        summary=(
            "Dispatches GPU video-render jobs, monitors completion, "
            "runs per-artifact QA, and drives tactical recovery."
        ),
        experiment_module="strands_agents.evals.experiments.production",
        cases_factory="production_cases",
        thresholds_attr="PRODUCTION_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_production_experiment",
        declared_models=(_OPENAI_4O, _GEMINI_3_PRO),
        task_attr="production_task",
    ),
    Component(
        id="c11",
        title="Assembly Agent",
        kind="tool",
        row=3,
        summary=(
            "Pure @tool. Assembles the OTIO timeline + renders the "
            "final mp4 via ffmpeg and uploads it to B2."
        ),
        experiment_module="strands_agents.evals.experiments.assembly",
        cases_factory="_make_cases",
        thresholds_attr="ASSEMBLY_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_assembly_experiment",
        declared_models=(),
        task_attr="assembly_task",
    ),
    Component(
        id="c12",
        title="Recovery Agents",
        kind="leaf",
        row=3,
        summary=(
            "SlidingWindowConversationManager agents that classify "
            "failures and decide fix / retry / skip."
        ),
        experiment_module="strands_agents.evals.experiments.recovery",
        cases_factory="_CASES",
        thresholds_attr="RECOVERY_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_recovery_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O, _KIMI_K2),
        task_attr="recovery_task",
    ),
    Component(
        id="c13",
        title="Escalation Supervisor",
        kind="leaf",
        row=3,
        summary=(
            "Top-level escalation handler. Decides between "
            "fix / retry / skip / escalate / abort and maintains the "
            "structured escalation record."
        ),
        experiment_module="strands_agents.evals.experiments.escalation",
        cases_factory="_CASES",
        thresholds_attr="ESCALATION_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_escalation_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        task_attr="escalation_task",
    ),
    Component(
        id="c14",
        title="Pipeline Graph",
        kind="graph",
        row=3,
        summary=(
            "Full composition. GraphBuilder stitching the 13 atomic "
            "components plus approval gates into the documentary "
            "pipeline."
        ),
        experiment_module="strands_agents.evals.experiments.pipeline",
        cases_factory="_CASES",
        thresholds_attr="PIPELINE_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_pipeline_experiment",
        declared_models=(_GEMINI_3_PRO, _OPENAI_4O),
        task_attr="pipeline_task",
    ),
    Component(
        id="c15",
        title="Approval Gates",
        kind="gate",
        row=3,
        summary=(
            "Interrupt-based human-in-the-loop. Pauses the graph at "
            "scenario, visual, and assembly boundaries and resumes on "
            "accept / edit / reject."
        ),
        experiment_module="strands_agents.evals.experiments.approval",
        cases_factory="_CASES",
        thresholds_attr="APPROVAL_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_approval_experiment",
        declared_models=(),
        task_attr="approval_task",
    ),
    # Infrastructure units — occupy row 4 in the UI (rendered under a
    # separate ``kind=infra`` filter chip). These are the VM-level
    # services the orchestrator composes the pipeline on top of.
    Component(
        id="infra_guardian",
        title="Guardian (cost control)",
        kind="infra",
        row=4,
        summary=(
            "Pure decision core for per-VM self-destruct: idle "
            "timeout, lifetime cap, manual-destroy flag. Pins the "
            "cost-control invariant the orchestrator trusts to keep "
            "unattended workers from burning budget."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_guardian"),
        cases_factory="infra_guardian_cases",
        thresholds_attr="INFRA_GUARDIAN_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_infra_guardian_experiment",
        declared_models=(),
        task_attr="infra_guardian_task",
    ),
    Component(
        id="infra_worker_registry",
        title="Worker registry",
        kind="infra",
        row=4,
        summary=(
            "Fleet registry + voice pinning. Enforces the "
            "one-voice-per-VM invariant and refuses VRAM-underprovisioned "
            "workers at registration."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_worker_registry"),
        cases_factory="infra_worker_registry_cases",
        thresholds_attr=("INFRA_WORKER_REGISTRY_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_worker_registry_experiment"),
        declared_models=(),
        task_attr="infra_worker_registry_task",
    ),
    Component(
        id="infra_agent",
        title="Infra agent (per-VM control plane)",
        kind="infra",
        row=4,
        summary=(
            "FastAPI on every worker VM — /infra/status, /infra/bump, "
            "/infra/destroy. Bumped by worker request middleware, "
            "latches manual-destroy, reports VRAM + disk peaks."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_agent"),
        cases_factory="infra_agent_cases",
        thresholds_attr="INFRA_AGENT_EVALUATOR_THRESHOLDS",
        experiment_builder_attr="build_infra_agent_experiment",
        declared_models=(),
        task_attr="infra_agent_task",
    ),
    Component(
        id="infra_qwen3_tts_worker",
        title="Qwen3-TTS worker",
        kind="infra",
        row=4,
        summary=(
            "Per-VM TTS worker. /tts/render returns a 16-bit mono PCM "
            "WAV for the VM's pinned voice; /health/vram surfaces "
            "peak VRAM. One voice per VM, deterministic for seed."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_qwen3_tts_worker"),
        cases_factory="infra_qwen3_tts_worker_cases",
        thresholds_attr=("INFRA_QWEN3_TTS_WORKER_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_qwen3_tts_worker_experiment"),
        declared_models=(),
        task_attr="infra_qwen3_tts_worker_task",
    ),
    Component(
        id="infra_ltx_video_worker",
        title="LTX-Video worker",
        kind="infra",
        row=4,
        summary=(
            "Per-VM video worker. /video/render returns an ISO-BMFF "
            "MP4 (ftyp + mdat), duration clamped to engine bounds, "
            "deterministic for seed. /health/vram surfaces peak VRAM."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_ltx_video_worker"),
        cases_factory="infra_ltx_video_worker_cases",
        thresholds_attr=("INFRA_LTX_VIDEO_WORKER_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_ltx_video_worker_experiment"),
        declared_models=(),
        task_attr="infra_ltx_video_worker_task",
    ),
    Component(
        id="infra_ltx_video_worker_live",
        title="LTX-Video worker (LIVE H200)",
        kind="infra",
        row=4,
        summary=(
            "Live passthrough to the LTX-2.3 BASIC engine on a real "
            "H200. POSTs /video/render at $LTX_VIDEO_WORKER_URL and "
            "asserts the response carries an ISO-BMFF mp4_base64 "
            "payload above the real-render byte floor (50 KB). Fails "
            "closed if the response engine field is 'stub'."
        ),
        experiment_module=(
            "strands_agents.evals.experiments.infra_ltx_video_worker_live"
        ),
        cases_factory="infra_ltx_video_worker_live_cases",
        thresholds_attr=("INFRA_LTX_VIDEO_WORKER_LIVE_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_ltx_video_worker_live_experiment"),
        declared_models=(),
        task_attr="infra_ltx_video_worker_live_task",
    ),
    Component(
        id="infra_b2_checkpoint",
        title="B2 checkpoint store",
        kind="infra",
        row=4,
        summary=(
            "Per-run artifact ledger + resume. Content-addressed, "
            "idempotent uploads; monotonic revision tags; fail-closed "
            "on checksum mismatch. The invariant the orchestrator "
            "leans on to make any run resumable."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_b2_checkpoint"),
        cases_factory="infra_b2_checkpoint_cases",
        thresholds_attr=("INFRA_B2_CHECKPOINT_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_b2_checkpoint_experiment"),
        declared_models=(),
        task_attr="infra_b2_checkpoint_task",
    ),
    Component(
        id="infra_pipeline_adapter",
        title="Pipeline playground adapter",
        kind="infra",
        row=4,
        summary=(
            "Pure translator from orchestrator AG-UI events onto the "
            "playground RunStream. Guarantees stable kind vocabulary, "
            "stage-bracket integrity, and the 'never drop' invariant "
            "for unknown event types."
        ),
        experiment_module=("strands_agents.evals.experiments.infra_pipeline_adapter"),
        cases_factory="infra_pipeline_adapter_cases",
        thresholds_attr=("INFRA_PIPELINE_ADAPTER_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_pipeline_adapter_experiment"),
        declared_models=(),
        task_attr="infra_pipeline_adapter_task",
    ),
    Component(
        id="infra_pipeline_live_orchestrator",
        title="Pipeline live orchestrator (scripted LLM)",
        kind="infra",
        row=4,
        summary=(
            "Drives the real DeepAgent orchestrator end-to-end with a "
            "scripted LLM (no GPU, no token spend). Proves every "
            "approval gate fires, every stage brackets, and the final "
            "MP4 URL is recoverable. The wiring proof for the live "
            "/pipeline form."
        ),
        experiment_module=(
            "strands_agents.evals.experiments.infra_pipeline_live_orchestrator"
        ),
        cases_factory="infra_pipeline_live_orchestrator_cases",
        thresholds_attr=("INFRA_PIPELINE_LIVE_ORCHESTRATOR_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_infra_pipeline_live_orchestrator_experiment"),
        declared_models=(),
        task_attr="infra_pipeline_live_orchestrator_task",
    ),
    Component(
        id="qa_video_artifact_probe",
        title="QA: video artifact probe",
        kind="gate",
        row=4,
        summary=(
            "ffprobe wrapper that reports an MP4's duration, codec, "
            "dimensions, and on-disk size. The deterministic floor "
            "every QA gate built on top of ffprobe shares; surfaced "
            "as a card so the orchestrator and the user inspect "
            "the same envelope."
        ),
        experiment_module=("strands_agents.evals.experiments.qa_video_artifact_probe"),
        cases_factory="qa_video_artifact_probe_cases",
        thresholds_attr=("QA_VIDEO_ARTIFACT_PROBE_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_qa_video_artifact_probe_experiment"),
        declared_models=(),
        task_attr="qa_video_artifact_probe_task",
    ),
    Component(
        id="qa_duration_align",
        title="QA: audio/video duration align",
        kind="gate",
        row=4,
        summary=(
            "Hard-fails when |audio_dur - video_dur| > tolerance "
            "(default 0.5 s). Catches the slice 9j frozen-frame "
            "regression: a 3.7 s LTX-2.3 clip paired with 13 s of "
            "narration trips delta=9.3 s and the run never reaches "
            "assembly."
        ),
        experiment_module=("strands_agents.evals.experiments.qa_duration_align"),
        cases_factory="qa_duration_align_cases",
        thresholds_attr=("QA_DURATION_ALIGN_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_qa_duration_align_experiment"),
        declared_models=(),
        task_attr="qa_duration_align_task",
    ),
    Component(
        id="qa_stills_judge",
        title="QA: stills judge (frozen-frame guard)",
        kind="gate",
        row=4,
        summary=(
            "Decodes N evenly-spaced frames and hard-fails when the "
            "mean L1 inter-frame pixel delta drops below the floor. "
            "Catches a video that has the right duration but is "
            "visually frozen — the muxer padded the last frame, or "
            "LTX-2.3 emitted N near-identical frames. Deterministic "
            "and credential-free."
        ),
        experiment_module=("strands_agents.evals.experiments.qa_stills_judge"),
        cases_factory="qa_stills_judge_cases",
        thresholds_attr=("QA_STILLS_JUDGE_EVALUATOR_THRESHOLDS"),
        experiment_builder_attr=("build_qa_stills_judge_experiment"),
        declared_models=(),
        task_attr="qa_stills_judge_task",
    ),
)


def iter_components() -> Iterator[Component]:
    """Yield every registered component in atlas order."""
    yield from _COMPONENTS


def get_component(component_id: str) -> Component | None:
    """Return the component with ``component_id`` or ``None``."""
    for component in _COMPONENTS:
        if component.id == component_id:
            return component
    return None


__all__ = [
    "COMPONENT_IDS",
    "INFRA_COMPONENT_IDS",
    "Component",
    "DeclaredModel",
    "EvaluatorDeclaration",
    "get_component",
    "iter_components",
]
