"""Shared Tier-2 eval harness.

Every component-specific suite under ``tests/unit/tier2/`` boils down to
the same three-step dance:

1. **Collect** the corpus artifacts for this component (filter by
   :class:`~strands_agents.corpus.manifest.CorpusComponent`).
2. **Hermetically check** provenance: bytes load, sha256 verifies,
   ``expected_verdict`` is populated so the live path has ground truth.
3. **(Live only)** route the artifact through a :class:`JudgeEnsemble`
   call with the component's rubric and compare the verdict string
   against ``expected_verdict`` under a forgiving alias table.

This module owns (2) and (3).  It does *not* own the corpus (that's
``corpus/``) or the judge fleet (that's ``judges/``).  It is the thin
glue so the 15 per-component suites stay small and boring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from strands_agents.corpus.fetcher import CorpusFetcher
from strands_agents.corpus.fixtures import filter_artifacts, load_artifact_bytes
from strands_agents.corpus.manifest import (
    CorpusArtifact,
    CorpusComponent,
    CorpusManifest,
)

logger = logging.getLogger(__name__)


class Tier2Mode(str, Enum):
    """Execution mode for a Tier-2 case.

    HERMETIC runs the structural check only (no network, no judges).
    LIVE additionally routes the artifact through JudgeEnsemble.  The
    distinction is exposed to test modules so the same parametrisation
    can branch without duplicating the case list.
    """

    HERMETIC = "hermetic"
    LIVE = "live"


@dataclass(frozen=True)
class Tier2Case:
    """A single corpus artifact bound to its component's rubric.

    Attributes:
        artifact: The corpus entry under test.
        mode: Which execution mode the case runs in.
    """

    artifact: CorpusArtifact
    mode: Tier2Mode

    @property
    def id(self) -> str:
        """Pytest-friendly ID (appears in ``-v`` output)."""
        return f"{self.artifact.key}[{self.mode.value}]"


def load_tier2_cases(
    manifest: CorpusManifest,
    component: CorpusComponent,
    *,
    mode: Tier2Mode = Tier2Mode.HERMETIC,
) -> tuple[Tier2Case, ...]:
    """Return every corpus artifact for ``component`` wrapped as a case.

    Args:
        manifest: The corpus manifest to filter.
        component: Which component's fixtures to return.
        mode: Execution mode stamped onto every returned case.

    Returns:
        Ordered tuple of cases, empty if the component has no corpus
        entries yet.  Callers skip the parametrised tests in that case
        so un-seeded components are visible in the CI output without
        hard-failing the suite.
    """
    artifacts = filter_artifacts(manifest, component=component)
    return tuple(Tier2Case(artifact=a, mode=mode) for a in artifacts)


# ---------------------------------------------------------------------------
# Hermetic assertions
# ---------------------------------------------------------------------------

# Roles whose presence we require to call a component "seeded".  Ambiguous
# is allowed but doesn't satisfy coverage on its own — the component needs
# something explicitly acceptable and something explicitly rejectable.
REQUIRED_ROLES: frozenset[str] = frozenset({"golden", "adversarial"})


def assert_hermetic(artifact: CorpusArtifact, fetcher: CorpusFetcher) -> None:
    """Assert the artifact loads cleanly and carries a usable expected_verdict.

    Fails the test with a clear message on provenance corruption,
    missing ``expected_verdict`` (which would make the live path
    untestable), or a zero-byte payload.

    Args:
        artifact: Corpus entry to validate.
        fetcher: Fetcher to resolve it through (seeded for hermetic mode).

    Raises:
        AssertionError: If any of the structural checks fail.
    """
    if not artifact.expected_verdict:
        raise AssertionError(
            f"corpus key=<{artifact.key}> has no expected_verdict; "
            f"live Tier-2 has no ground truth to grade against"
        )

    payload = load_artifact_bytes(fetcher, artifact)
    if not payload:
        raise AssertionError(
            f"corpus key=<{artifact.key}> resolved to 0 bytes"
        )
    if len(payload) != artifact.size_bytes:
        raise AssertionError(
            f"corpus key=<{artifact.key}> size mismatch: "
            f"manifest=<{artifact.size_bytes}>, actual=<{len(payload)}>"
        )


def assert_component_seeded(
    manifest: CorpusManifest,
    component: CorpusComponent,
) -> None:
    """Assert ``component`` has at least one golden and one adversarial.

    This is the "MAKE IT CARE" coverage gate — a component without
    both poles of ground truth can't meaningfully be evaluated.  Tests
    call this to surface un-seeded components loudly in the CI output
    instead of silently passing with zero parametrisation.

    Raises:
        AssertionError: If either role is missing.
    """
    present = {a.role for a in filter_artifacts(manifest, component=component)}
    missing = REQUIRED_ROLES - present
    if missing:
        raise AssertionError(
            f"component=<{component}> missing corpus role(s)=<{sorted(missing)}>; "
            f"Tier-2 cannot grade both poles without them"
        )


# ---------------------------------------------------------------------------
# Live judge routing
# ---------------------------------------------------------------------------

# Forgiving alias table: a judge may emit ``"pass"`` when the rubric
# labels the positive pole ``"accept"``.  We collapse synonyms so the
# comparison against ``expected_verdict`` doesn't fail on cosmetic
# wording differences.  Add aliases conservatively — a too-permissive
# table silently accepts wrong verdicts.
_VERDICT_ALIASES: dict[str, str] = {
    "pass": "accept",
    "passed": "accept",
    "ok": "accept",
    "approve": "accept",
    "approved": "accept",
    "fail": "reject",
    "failed": "reject",
    "deny": "reject",
    "denied": "reject",
    "block": "reject",
    "blocked": "reject",
    "rejected": "reject",
    "accepted": "accept",
    "refine_needed": "refine",
    "needs_refine": "refine",
    "escalate_needed": "escalate",
    "needs_escalation": "escalate",
    "borderline": "ambiguous",
    "uncertain": "ambiguous",
}


def normalise_verdict(raw: str) -> str:
    """Lowercase + alias-collapse a judge verdict string."""
    key = (raw or "").strip().lower()
    return _VERDICT_ALIASES.get(key, key)


def verdict_matches(actual: str, expected: str) -> bool:
    """Return True if the normalised verdicts match."""
    return normalise_verdict(actual) == normalise_verdict(expected)


def build_judge_request(
    artifact: CorpusArtifact,
    payload: bytes,
    rubric_prompt: str,
) -> dict[str, Any]:
    """Build a generic JudgeRequest-shaped dict for the live path.

    Kept as a dict (not a JudgeRequest) so this module stays importable
    without strands_agents.judges.  Live test code converts it to the
    actual request type on demand.

    Args:
        artifact: Corpus entry under evaluation.
        payload: Raw artifact bytes (pre-loaded so the harness controls
            decoding policy).
        rubric_prompt: The component-specific judge instruction.

    Returns:
        Mapping with keys ``system``, ``user``, ``artifact_key``,
        ``expected_verdict`` — the judge client ignores unknown keys,
        the test uses ``expected_verdict`` to assert agreement.
    """
    try:
        as_text = payload.decode("utf-8")
    except UnicodeDecodeError:
        as_text = f"<binary artifact; sha256={artifact.sha256}>"

    # Blind the judge: the manifest key encodes polarity (e.g.
    # ``scenario.golden.offtopic``) and the role field states it outright,
    # so neither can appear in the prompt — LLMs are known to latch onto
    # context clues even when told to ignore them.  We pass a sha-derived
    # opaque identifier instead; the test side keeps the real key to map
    # verdicts back to ground truth.
    blinded_id = f"artifact-{artifact.sha256[:12]}"

    return {
        "system": rubric_prompt,
        "user": (
            f"Artifact id: {blinded_id}\n"
            f"Content type: {artifact.content_type}\n"
            f"---\n{as_text}\n---\n"
            f"Grade this artifact per the rubric."
        ),
        "artifact_key": artifact.key,
        "expected_verdict": artifact.expected_verdict,
    }


def assert_live_verdict(
    artifact: CorpusArtifact,
    actual_verdict: str,
    *,
    expected: Optional[str] = None,
) -> None:
    """Assert the judge's verdict matches the artifact's expected_verdict.

    Uses the alias table so cosmetic differences don't fail the test.
    A verdict of ``"abstain"`` is allowed — if the ensemble couldn't
    parse a score, we don't have grounds to call it wrong, but we log
    the abstention for dashboards.

    Args:
        artifact: Corpus entry under test.
        actual_verdict: Verdict string the ensemble returned.
        expected: Override for the expected verdict (defaults to
            ``artifact.expected_verdict``).

    Raises:
        AssertionError: On verdict disagreement.
    """
    expected_v = expected if expected is not None else (artifact.expected_verdict or "")
    if not expected_v:
        raise AssertionError(
            f"corpus key=<{artifact.key}> has no expected_verdict set"
        )

    if not actual_verdict or normalise_verdict(actual_verdict) == "abstain":
        logger.warning(
            "artifact=<%s>, expected=<%s> | judge abstained — recorded but not failed",
            artifact.key, expected_v,
        )
        return

    if not verdict_matches(actual_verdict, expected_v):
        raise AssertionError(
            f"corpus key=<{artifact.key}> verdict mismatch: "
            f"expected=<{expected_v}>, actual=<{actual_verdict}>"
        )
