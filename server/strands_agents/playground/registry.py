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

import importlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from strands_evals.case import Case


#: Well-known identifiers. Ordering mirrors the test-case atlas rows.
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
        self._cache["cases"] = cases
        return cases

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


#: Internal sentinel so the ``task()`` cache can distinguish "never
#: checked" from "checked, not available". ``None`` would collide with
#: the public "no task adapter" semantic.
_MISSING_TASK = object()


_GEMINI_3_1: DeclaredModel = DeclaredModel(
    id="gemini/gemini-3.1-pro", provider="gemini", role="canonical"
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O, _KIMI_K2),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(),  # TTS + WhisperX, not an LLM agent
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(_GEMINI_3_1, _QWEN_OMNI, _GEMMA_4_URC),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(_OPENAI_4O, _GEMINI_3_1),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O, _KIMI_K2),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(_GEMINI_3_1, _OPENAI_4O),
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
        declared_models=(),
        task_attr="approval_task",
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
    "Component",
    "DeclaredModel",
    "EvaluatorDeclaration",
    "get_component",
    "iter_components",
]
