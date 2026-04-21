"""Corpus manifest types and loader.

The corpus is the set of real artifacts (scenarios, audio samples, video
frames, OTIO timelines, critiques) that the atomic-robustness evals grade
components against.  Artifacts are too large or license-encumbered to
commit, so they live in B2 and are fetched on demand, cached locally by
content hash.  The manifest is the source of truth for:

- which artifacts exist
- which component they belong to (01-15)
- which role they play in an eval (golden / adversarial / ambiguous)
- the expected verdict the component under test should return
- the B2 key + sha256 + size for content-addressed fetch
- the license + source so we can prove provenance

The manifest itself is small JSON that lives in the repo; the binary
artifacts are opaque blobs fetched by the ``corpus.fetcher`` module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# The component a corpus artifact targets.  Mirrors the component numbers
# in ``docs/strands-migration/components/`` so every eval can trivially
# filter "just my component's fixtures".
CorpusComponent = Literal[
    "01-scenario-agent",
    "02-timing-evaluator",
    "03-scenario-refiner",
    "04-audio-agent",
    "05-timing-loop",
    "06-content-analyst",
    "07-visual-concepter",
    "08-coherence-evaluator",
    "09-visual-loop",
    "10-production-supervisor",
    "11-assembly-agent",
    "12-recovery-agents",
    "13-escalation-supervisor",
    "14-pipeline-graph",
    "15-approval-gates",
]

# The role an artifact plays in its eval.  ``golden`` artifacts MUST pass
# the component's checks; ``adversarial`` artifacts MUST be rejected;
# ``ambiguous`` artifacts exist to probe judgment quality — the expected
# verdict is explicit and the scorer checks for agreement with human
# ground truth, not any particular polarity.
CorpusRole = Literal["golden", "adversarial", "ambiguous"]

# Content type of the artifact's bytes.  Used to choose the right loader
# in the fixture helpers; evaluators dispatch on this.
CorpusContentType = Literal[
    # Pipeline artifacts (JSON)
    "scenario_json",
    "timing_report_json",
    "refined_scenario_json",
    "audio_qa_report_json",
    "otio_summary_json",
    "critique_json",
    "content_analysis_json",
    "visual_concept_json",
    "loop_trace_json",
    "production_plan_json",
    "recovery_decision_json",
    "escalation_decision_json",
    "pipeline_trace_json",
    "approval_request_json",
    # Binary media
    "audio_wav",
    "audio_mp3",
    "video_mp4",
    "video_frame_png",
    "otio_xml",
]

# Storage scheme for an artifact's bytes.  ``seed`` means the bytes are
# committed under ``corpus/seeds/`` so hermetic tests work without B2;
# ``b2`` means the bytes live in the cloud and require ``B2_KEY_ID`` +
# ``B2_APPLICATION_KEY`` to fetch.
CorpusStorage = Literal["seed", "b2"]


@dataclass(frozen=True)
class CorpusArtifact:
    """Metadata for a single corpus artifact.

    Content-addressed by :attr:`sha256`, which is verified on every
    fetch — a mismatch is treated as cache corruption and the artifact
    is re-downloaded.  ``expected_verdict`` is optional because some
    artifacts are probes where the "correct" output isn't a label but a
    reasoning quality.
    """

    key: str
    component: CorpusComponent
    role: CorpusRole
    content_type: CorpusContentType
    storage: CorpusStorage
    sha256: str
    size_bytes: int
    b2_key: Optional[str] = None
    seed_path: Optional[str] = None
    expected_verdict: Optional[str] = None
    notes: str = ""
    license: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        """Validate storage-specific fields are populated consistently."""
        if self.storage == "seed" and not self.seed_path:
            raise ValueError(
                f"corpus artifact key=<{self.key}> has storage=<seed> "
                f"but no seed_path"
            )
        if self.storage == "b2" and not self.b2_key:
            raise ValueError(
                f"corpus artifact key=<{self.key}> has storage=<b2> "
                f"but no b2_key"
            )
        if len(self.sha256) != 64:
            raise ValueError(
                f"corpus artifact key=<{self.key}> has invalid "
                f"sha256=<{self.sha256}> (expected 64 hex chars)"
            )
        if self.size_bytes <= 0:
            raise ValueError(
                f"corpus artifact key=<{self.key}> has invalid "
                f"size_bytes=<{self.size_bytes}>"
            )


@dataclass(frozen=True)
class CorpusManifest:
    """A collection of :class:`CorpusArtifact` entries with lookups.

    The manifest carries an explicit :attr:`version` so we can evolve
    the schema without silently breaking clients.  Adding new fields is
    backwards-compatible (clients ignore unknown keys); removing or
    renaming fields requires a version bump.
    """

    version: int
    artifacts: tuple[CorpusArtifact, ...]

    _by_key: dict[str, CorpusArtifact] = field(default_factory=dict, init=False, repr=False)
    _by_component: dict[str, tuple[CorpusArtifact, ...]] = field(
        default_factory=dict, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        """Build by-key / by-component lookup tables."""
        by_key: dict[str, CorpusArtifact] = {}
        by_component: dict[str, list[CorpusArtifact]] = {}
        for artifact in self.artifacts:
            if artifact.key in by_key:
                raise ValueError(
                    f"corpus manifest has duplicate key=<{artifact.key}>"
                )
            by_key[artifact.key] = artifact
            by_component.setdefault(artifact.component, []).append(artifact)
        # ``frozen=True`` forbids ``self.foo = ...`` but object.__setattr__
        # is legal inside ``__post_init__``.
        object.__setattr__(self, "_by_key", by_key)
        object.__setattr__(
            self,
            "_by_component",
            {k: tuple(v) for k, v in by_component.items()},
        )

    def get(self, key: str) -> CorpusArtifact:
        """Return the artifact with ``key``.

        Raises:
            KeyError: if no such key exists.
        """
        if key not in self._by_key:
            raise KeyError(f"corpus manifest has no artifact with key=<{key}>")
        return self._by_key[key]

    def for_component(self, component: CorpusComponent) -> tuple[CorpusArtifact, ...]:
        """Return all artifacts targeting ``component`` (empty if none)."""
        return self._by_component.get(component, ())

    def by_role(
        self,
        component: CorpusComponent,
        role: CorpusRole,
    ) -> tuple[CorpusArtifact, ...]:
        """Return artifacts for ``component`` filtered to ``role``."""
        return tuple(a for a in self.for_component(component) if a.role == role)


def load_manifest(path: Path | str) -> CorpusManifest:
    """Load a :class:`CorpusManifest` from a JSON file.

    Args:
        path: Filesystem path to a JSON manifest.

    Returns:
        Parsed :class:`CorpusManifest`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the manifest fails schema validation.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"corpus manifest not found at <{p}>")

    with p.open() as fh:
        raw = json.load(fh)

    return _manifest_from_dict(raw, manifest_path=p)


def _manifest_from_dict(
    raw: dict[str, Any],
    *,
    manifest_path: Optional[Path] = None,
) -> CorpusManifest:
    """Materialise a :class:`CorpusManifest` from a parsed-JSON dict."""
    version = raw.get("version")
    if not isinstance(version, int):
        raise ValueError("corpus manifest missing integer <version> field")

    raw_artifacts = raw.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ValueError("corpus manifest missing list <artifacts> field")

    artifacts: list[CorpusArtifact] = []
    for i, item in enumerate(raw_artifacts):
        if not isinstance(item, dict):
            raise ValueError(
                f"corpus manifest artifacts[{i}] is not an object"
            )
        artifacts.append(_artifact_from_dict(item, index=i))

    logger.debug(
        "path=<%s>, version=<%d>, count=<%d> | loaded corpus manifest",
        manifest_path, version, len(artifacts),
    )
    return CorpusManifest(version=version, artifacts=tuple(artifacts))


def _artifact_from_dict(raw: dict[str, Any], *, index: int) -> CorpusArtifact:
    """Materialise a :class:`CorpusArtifact` from a parsed-JSON dict."""
    required = ("key", "component", "role", "content_type", "storage", "sha256", "size_bytes")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(
            f"corpus manifest artifacts[{index}] missing fields=<{missing}>"
        )

    return CorpusArtifact(
        key=str(raw["key"]),
        component=raw["component"],
        role=raw["role"],
        content_type=raw["content_type"],
        storage=raw["storage"],
        sha256=str(raw["sha256"]),
        size_bytes=int(raw["size_bytes"]),
        b2_key=raw.get("b2_key"),
        seed_path=raw.get("seed_path"),
        expected_verdict=raw.get("expected_verdict"),
        notes=str(raw.get("notes", "")),
        license=str(raw.get("license", "")),
        source=str(raw.get("source", "")),
    )
