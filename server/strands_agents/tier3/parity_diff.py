"""OTIO parity diff between two pipeline paths.

Given two OTIO timelines produced by independent pipeline runs — one
from the strands path, one from the ADK baseline, on the same topic —
this module produces a structured report of the ways they differ.

What counts as "parity"
-----------------------
End-to-end parity is not byte-identical OTIO.  Two independent runs
will always produce different UUIDs, different media-reference paths,
and slightly different clip boundaries (WhisperX alignment is not
deterministic across runs).  Parity means:

* **Scene count matches.** The scenario director on both paths must
  pick the same number of narrative beats for the same topic.  A
  mismatch is a structural divergence, always red.
* **Per-scene duration matches within tolerance.** The timing loop on
  both paths must converge to durations within ``DURATION_TOLERANCE_SEC``
  (default 2.0 s, same as the cutover gate specified in
  :mod:`strands_agents.tools.assembly_tool`).
* **Track topology matches.** Both timelines must contain a narration
  track and a video track, and the clip-count relationship must hold
  (equal, or within 1 for a framing-card edge case).

Anything else — track names, media-reference URLs, per-clip UUIDs —
is allowed to differ.

Consumers
---------
The diff is consumed by the tier-3 harness
(:mod:`strands_agents.tier3.end_to_end_harness`) and by the
on-demand tier-3 CI workflow.  It is deliberately hermetic: given
two OTIO files on disk, it produces a deterministic report with no
external dependencies beyond ``opentimelineio``.
"""

from __future__ import annotations

import enum
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


DURATION_TOLERANCE_SEC: float = 2.0
"""Default per-scene duration tolerance, matches the cutover gate."""

CLIP_COUNT_TOLERANCE: int = 1
"""How much track clip-counts may differ between the two pipelines."""


class ParitySeverity(str, enum.Enum):
    """Severity tier for a single parity finding.

    ``RED`` findings block the parity gate.  ``YELLOW`` findings are
    informational — they surface in the report but do not fail the
    tier-3 job on their own.  ``GREEN`` is reserved for the summary
    record; no individual finding is emitted at green.
    """

    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"


@dataclass(frozen=True)
class ParityFinding:
    """A single divergence between the two pipelines."""

    category: str
    severity: ParitySeverity
    detail: str
    strands_value: Any = None
    adk_value: Any = None


@dataclass(frozen=True)
class ParityDiff:
    """Full parity report between two timelines."""

    strands_path: str
    adk_path: str
    findings: tuple[ParityFinding, ...] = field(default_factory=tuple)

    @property
    def is_red(self) -> bool:
        """Return ``True`` iff at least one finding is RED severity."""
        return any(f.severity is ParitySeverity.RED for f in self.findings)

    @property
    def is_yellow(self) -> bool:
        """Return ``True`` iff at least one finding is YELLOW severity."""
        return any(f.severity is ParitySeverity.YELLOW for f in self.findings)

    def format(self) -> str:
        """Render a compact, grep-friendly summary of the diff."""
        if not self.findings:
            return (
                f"Parity diff {self.strands_path} vs {self.adk_path}: "
                "all checks green."
            )
        severity = "RED" if self.is_red else "YELLOW"
        lines = [
            f"Parity diff ({severity}) {self.strands_path} vs {self.adk_path}:"
        ]
        for f in self.findings:
            lines.append(
                f"  - [{f.severity.value}] {f.category}: {f.detail} "
                f"(strands={f.strands_value!r} adk={f.adk_value!r})"
            )
        return "\n".join(lines)


def compare_timelines(
    strands_path: str | pathlib.Path,
    adk_path: str | pathlib.Path,
    *,
    duration_tolerance_sec: float = DURATION_TOLERANCE_SEC,
    clip_count_tolerance: int = CLIP_COUNT_TOLERANCE,
) -> ParityDiff:
    """Compare two OTIO timelines and return a parity diff.

    Args:
        strands_path: OTIO timeline produced by the strands pipeline.
        adk_path: OTIO timeline produced by the ADK pipeline.
        duration_tolerance_sec: Allowed per-scene duration divergence.
        clip_count_tolerance: Allowed per-track clip-count divergence.

    Returns:
        A :class:`ParityDiff` with one finding per detected divergence.

    Raises:
        FileNotFoundError: If either timeline file is missing.
        ImportError: If ``opentimelineio`` is not installed.
        ValueError: If either timeline fails to parse.
    """
    s_path = pathlib.Path(strands_path)
    a_path = pathlib.Path(adk_path)
    for label, p in (("strands", s_path), ("adk", a_path)):
        if not p.exists():
            msg = f"{label} timeline not found: {p}"
            raise FileNotFoundError(msg)

    otio = _import_otio()
    s_tl = _load_timeline(otio, s_path)
    a_tl = _load_timeline(otio, a_path)

    findings: list[ParityFinding] = []
    findings.extend(_scene_count_findings(s_tl, a_tl, otio))
    findings.extend(
        _duration_findings(
            s_tl, a_tl, otio, tolerance_sec=duration_tolerance_sec
        )
    )
    findings.extend(
        _topology_findings(
            s_tl, a_tl, otio, clip_count_tolerance=clip_count_tolerance
        )
    )
    return ParityDiff(
        strands_path=str(s_path),
        adk_path=str(a_path),
        findings=tuple(findings),
    )


def _import_otio() -> Any:
    try:
        import opentimelineio as otio
    except ImportError as exc:  # pragma: no cover — dep pinned in pyproject
        msg = f"opentimelineio unavailable: {exc}"
        raise ImportError(msg) from exc
    return otio


def _load_timeline(otio: Any, path: pathlib.Path) -> Any:
    try:
        return otio.adapters.read_from_file(str(path))
    except Exception as exc:
        msg = f"failed to parse timeline at {path}: {exc}"
        raise ValueError(msg) from exc


def _narration_track(tl: Any, otio: Any) -> Any | None:
    for t in tl.tracks:
        if "narration" in (t.name or "").lower():
            return t
    # Fallback: first audio track.
    for t in tl.tracks:
        if t.kind == otio.schema.TrackKind.Audio:
            return t
    return None


def _video_track(tl: Any, otio: Any) -> Any | None:
    for t in tl.tracks:
        if "video" in (t.name or "").lower():
            return t
    for t in tl.tracks:
        if t.kind == otio.schema.TrackKind.Video:
            return t
    return None


def _clip_durations(track: Any, otio: Any) -> list[float]:
    durations: list[float] = []
    for item in track:
        if isinstance(item, otio.schema.Clip):
            sr = item.source_range
            if sr is not None:
                durations.append(sr.duration.to_seconds())
    return durations


def _scene_count_findings(s_tl: Any, a_tl: Any, otio: Any) -> list[ParityFinding]:
    s_track = _narration_track(s_tl, otio)
    a_track = _narration_track(a_tl, otio)
    if s_track is None or a_track is None:
        # Narration track missing is a topology issue, not a scene-count
        # one — _topology_findings will surface it.
        return []
    s_count = len(_clip_durations(s_track, otio))
    a_count = len(_clip_durations(a_track, otio))
    if s_count == a_count:
        return []
    return [
        ParityFinding(
            category="scene_count",
            severity=ParitySeverity.RED,
            detail=(
                f"scene count mismatch: strands={s_count} adk={a_count}"
            ),
            strands_value=s_count,
            adk_value=a_count,
        )
    ]


def _duration_findings(
    s_tl: Any,
    a_tl: Any,
    otio: Any,
    *,
    tolerance_sec: float,
) -> list[ParityFinding]:
    s_track = _narration_track(s_tl, otio)
    a_track = _narration_track(a_tl, otio)
    if s_track is None or a_track is None:
        return []
    s_durations = _clip_durations(s_track, otio)
    a_durations = _clip_durations(a_track, otio)
    # Only compare up to the shorter list — the scene-count finding
    # already flagged the length mismatch if present.
    out: list[ParityFinding] = []
    for idx, (s_d, a_d) in enumerate(zip(s_durations, a_durations, strict=False)):
        delta = abs(s_d - a_d)
        if delta > tolerance_sec:
            out.append(
                ParityFinding(
                    category="scene_duration",
                    severity=ParitySeverity.RED,
                    detail=(
                        f"scene[{idx}] duration diverges by "
                        f"{delta:.2f}s > {tolerance_sec:.2f}s"
                    ),
                    strands_value=round(s_d, 3),
                    adk_value=round(a_d, 3),
                )
            )
    # Total-duration divergence within tolerance = yellow flag.
    s_total = sum(s_durations)
    a_total = sum(a_durations)
    total_delta = abs(s_total - a_total)
    if total_delta > tolerance_sec and not out:
        out.append(
            ParityFinding(
                category="total_duration",
                severity=ParitySeverity.YELLOW,
                detail=(
                    f"total duration diverges by {total_delta:.2f}s "
                    f"(per-scene tolerance fine)"
                ),
                strands_value=round(s_total, 3),
                adk_value=round(a_total, 3),
            )
        )
    return out


def _topology_findings(
    s_tl: Any,
    a_tl: Any,
    otio: Any,
    *,
    clip_count_tolerance: int,
) -> list[ParityFinding]:
    findings: list[ParityFinding] = []

    for label, tl in (("strands", s_tl), ("adk", a_tl)):
        if _narration_track(tl, otio) is None:
            findings.append(
                ParityFinding(
                    category="topology",
                    severity=ParitySeverity.RED,
                    detail=f"{label} timeline missing narration track",
                    strands_value=label == "strands",
                    adk_value=label == "adk",
                )
            )
        if _video_track(tl, otio) is None:
            findings.append(
                ParityFinding(
                    category="topology",
                    severity=ParitySeverity.RED,
                    detail=f"{label} timeline missing video track",
                    strands_value=label == "strands",
                    adk_value=label == "adk",
                )
            )

    s_video = _video_track(s_tl, otio)
    a_video = _video_track(a_tl, otio)
    if s_video is not None and a_video is not None:
        s_count = len(_clip_durations(s_video, otio))
        a_count = len(_clip_durations(a_video, otio))
        if abs(s_count - a_count) > clip_count_tolerance:
            findings.append(
                ParityFinding(
                    category="video_clip_count",
                    severity=ParitySeverity.YELLOW,
                    detail=(
                        f"video clip counts differ by more than "
                        f"{clip_count_tolerance}: strands={s_count} "
                        f"adk={a_count}"
                    ),
                    strands_value=s_count,
                    adk_value=a_count,
                )
            )
    return findings
