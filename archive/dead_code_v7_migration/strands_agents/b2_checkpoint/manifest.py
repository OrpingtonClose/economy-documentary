"""Manifest schema for the B2 checkpoint ledger.

One manifest per ``run_id``, stored in B2 at a well-known key. Each
entry records a single uploaded artifact: what it is, which revision
produced it, where it lives, and how to verify it. The manifest is an
append-only ledger — entries are never mutated or reordered in place.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


#: The kinds of artifacts the checkpoint store knows about. Adding a
#: new kind here is intentional friction — every downstream consumer
#: (orchestrator, resume loop, UI) checks ``kind`` exhaustively.
ArtifactKind = Literal[
    "scene_json",
    "audio_wav",
    "video_mp4",
    "timing_alignment",
    "otio_xml",
    "master_mp4",
]

ARTIFACT_KINDS: tuple[ArtifactKind, ...] = (
    "scene_json",
    "audio_wav",
    "video_mp4",
    "timing_alignment",
    "otio_xml",
    "master_mp4",
)


class ManifestEntry(BaseModel):
    """One uploaded artifact in a run's ledger.

    Attributes:
        artifact_id: Opaque content-addressed id the store mints on
            upload. Stable across re-uploads of the same bytes.
        run_id: The run that produced this artifact.
        revision_tag: The preference-ledger revision active when this
            artifact was produced. Monotonic per ``run_id``.
        kind: Artifact kind (see :data:`ARTIFACT_KINDS`).
        b2_key: The object key in B2 where the bytes live.
        sha256: Hex digest of the artifact bytes.
        size_bytes: Artifact size in bytes.
        uploaded_at_iso: RFC-3339 UTC timestamp of the upload.
        idempotency_key: The key the store derives from
            ``(run_id, kind, revision_tag, sha256)``. A duplicate
            upload with the same key and bytes returns the existing
            entry; a collision with different bytes is an error.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_tag: str = Field(min_length=1)
    kind: ArtifactKind
    b2_key: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    uploaded_at_iso: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class Manifest(BaseModel):
    """The full ledger for one ``run_id``.

    Entries are stored in upload order — a list, not a set. The resume
    loop and the playground UI both rely on that order to reconstruct
    the scene sequence and show the run history.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    entries: tuple[ManifestEntry, ...] = ()

    @property
    def latest_revision_tag(self) -> str | None:
        """Return the highest revision tag present, or ``None``."""
        if not self.entries:
            return None
        return max(entry.revision_tag for entry in self.entries)


def dumps(manifest: Manifest) -> bytes:
    """Serialise a :class:`Manifest` to canonical-ordered JSON bytes.

    Canonical ordering (sorted keys, no whitespace deltas) keeps the
    stored object byte-stable across Pydantic minor versions — which
    matters because the manifest itself is sometimes checksummed as
    part of a larger resume bundle.
    """
    payload: dict[str, Any] = manifest.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def loads(raw: bytes) -> Manifest:
    """Parse bytes produced by :func:`dumps` back into a :class:`Manifest`."""
    return Manifest.model_validate_json(raw.decode("utf-8"))


__all__ = ["ArtifactKind",
    "Manifest",
    "ManifestEntry",
    "dumps",
    "loads",]
