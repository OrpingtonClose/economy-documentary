"""Per-component test-module builder.

Each Tier-2 test module for components 01-15 is structurally identical:
filter the manifest to the component's artifacts, parametrise hermetic
+ live cases, and assert coverage.  Rather than repeat 60 lines fifteen
times, the modules call :func:`build_component_tests` with their
component key and receive the test functions back.

This keeps per-component modules ~15 lines each — just the component
identifier and module-level docstring.  Rubric drift or harness bugs
fix in one place.
"""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.corpus.fixtures import filter_artifacts, load_artifact_bytes
from strands_agents.corpus.manifest import CorpusArtifact, CorpusComponent
from strands_agents.tier2 import harness
from strands_agents.tier2.rubrics import get_rubric


def _artifacts_for(component: CorpusComponent) -> tuple[CorpusArtifact, ...]:
    """Module-import-time corpus filter.

    We look up the manifest eagerly so pytest's ``-v`` output lists one
    test per artifact.  The default manifest is committed and small, so
    this is a cheap dict lookup — no I/O at collection time beyond the
    one-shot manifest load.
    """
    from strands_agents.corpus.fixtures import load_default_manifest

    return filter_artifacts(load_default_manifest(), component=component)


def build_component_tests(component: CorpusComponent) -> dict[str, Any]:
    """Return a dict of test functions for ``component``.

    Test modules spread these into their module namespace::

        from strands_agents.tests.unit.tier2._builder import build_component_tests
        globals().update(build_component_tests("01-scenario-agent"))

    The returned dict contains:

    - ``test_corpus_seeded``: asserts both golden and adversarial poles exist.
      Marked as ``xfail(strict=False)`` so un-seeded components show up in
      the CI output without hard-failing the atomic-robustness gate.
    - ``test_hermetic_artifact_loads[key]``: parametrised hermetic structural
      check per artifact.
    - ``test_live_judge_matches_expected_verdict[key]``: parametrised live
      judge roundtrip, marked ``tier2_live``.  Skipped unless
      ``--tier2-live`` is passed.

    Args:
        component: Component key this module grades.

    Returns:
        Mapping of test function names to callables, suitable for
        ``globals().update(...)`` in the test module.
    """
    artifacts = _artifacts_for(component)
    rubric = get_rubric(component)

    def test_corpus_seeded() -> None:
        """Require a golden+adversarial pair for this component.

        xfail so un-seeded components are loud in CI but don't block
        the atomic-robustness gate.  Gradually flips to pass as
        corpus expands (tracked in PR-E.1 / PR-F).
        """
        present = {a.role for a in artifacts}
        missing = harness.REQUIRED_ROLES - present
        if missing:
            pytest.xfail(
                f"component=<{component}> missing corpus role(s)="
                f"<{sorted(missing)}>; track in PR-E.1 corpus expansion"
            )

    if artifacts:

        @pytest.mark.parametrize(
            "artifact",
            artifacts,
            ids=lambda a: a.key,
        )
        def test_hermetic_artifact_loads(
            artifact: CorpusArtifact,
            tier2_fetcher: Any,
        ) -> None:
            """Hermetic structural gate — bytes load, sha matches, verdict set."""
            harness.assert_hermetic(artifact, tier2_fetcher)

        @pytest.mark.tier2_live
        @pytest.mark.parametrize(
            "artifact",
            artifacts,
            ids=lambda a: a.key,
        )
        def test_live_judge_matches_expected_verdict(
            artifact: CorpusArtifact,
            tier2_fetcher: Any,
            tier2_judge_ensemble: Any,
        ) -> None:
            """Live-mode gate — judge verdict matches expected_verdict."""
            payload = load_artifact_bytes(tier2_fetcher, artifact)
            request_dict = harness.build_judge_request(
                artifact=artifact,
                payload=payload,
                rubric_prompt=rubric.prompt,
            )

            from strands_agents.judges.client import JudgeRequest

            request = JudgeRequest(
                system=request_dict["system"],
                user=request_dict["user"],
            )

            verdict = _route_ensemble(
                ensemble=tier2_judge_ensemble,
                judge_role=rubric.judge_role,
                request=request,
            )

            harness.assert_live_verdict(artifact, verdict.verdict)
    else:

        def test_hermetic_artifact_loads() -> None:  # type: ignore[misc]
            """No corpus yet for this component — tracked by test_corpus_seeded."""
            pytest.skip(f"no corpus artifacts for component=<{component}>")

        @pytest.mark.tier2_live
        def test_live_judge_matches_expected_verdict() -> None:  # type: ignore[misc]
            pytest.skip(f"no corpus artifacts for component=<{component}>")

    return {
        "test_corpus_seeded": test_corpus_seeded,
        "test_hermetic_artifact_loads": test_hermetic_artifact_loads,
        "test_live_judge_matches_expected_verdict": test_live_judge_matches_expected_verdict,
    }


def _route_ensemble(
    ensemble: Any,
    judge_role: str,
    request: Any,
) -> Any:
    """Dispatch a request to the right JudgeEnsemble role method."""
    method_name = f"query_{judge_role}"
    method = getattr(ensemble, method_name, None)
    if method is None:
        raise ValueError(
            f"JudgeEnsemble has no query_{judge_role} method; check rubric"
        )
    return method(request)
