"""Manifest and lookup helpers for committed media fixtures.

Every fixture the eval harness can run against is declared once, here.
The manifest is intentionally a plain JSON file on disk (not a Python
module) so that:

- fixture additions are reviewable as diffs,
- external tools (B2 upload script, CI fixture-drift check) can read
  it without importing the rest of the package,
- any change to a fixture's bytes is either reflected as a sha256
  change in the manifest or caught by the determinism test.

Each entry encodes the minimum needed to drive a judge case:

- ``id`` — stable identifier referenced by Experiment cases.
- ``axis`` — which clear-cut question the fixture exercises
  (``text_present``, ``color_dominance``, ``frozen_frame``, etc.).
- ``media`` — ``"video"`` or ``"audio"``.
- ``relative_path`` — path under ``fixtures/`` on disk.
- ``sha256`` — digest of the bytes. Drift from this is a hard error.
- ``expected_verdict`` — the clear-cut answer (``"yes"`` / ``"no"`` /
  ``"reject"``) the judge stack MUST return. If a judge flips it, the
  judge is a candidate for discard.
- ``prompt`` — the binary question asked of the judge; short, clear,
  answerable with a single word.
- ``public_url`` — optional mirror URL for providers (e.g. DashScope's
  OpenAI-compatible video endpoint) that refuse local files.
- ``generator`` — spec that produced the bytes. Used by the
  determinism test; may be ``None`` for fixtures pulled from external
  corpora.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FixtureEntry:
    """A single media fixture referenced by the eval harness."""

    id: str
    axis: str
    media: str
    relative_path: str
    sha256: str
    expected_verdict: str
    prompt: str
    public_url: str | None = None
    generator: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FixtureEntry":
        """Build a :class:`FixtureEntry` from a manifest dict entry."""
        return cls(
            id=raw["id"],
            axis=raw["axis"],
            media=raw["media"],
            relative_path=raw["relative_path"],
            sha256=raw["sha256"],
            expected_verdict=raw["expected_verdict"],
            prompt=raw["prompt"],
            public_url=raw.get("public_url"),
            generator=raw.get("generator"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a manifest-shaped dict for JSON writeout."""
        out: dict[str, Any] = {
            "id": self.id,
            "axis": self.axis,
            "media": self.media,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "expected_verdict": self.expected_verdict,
            "prompt": self.prompt,
        }
        if self.public_url is not None:
            out["public_url"] = self.public_url
        if self.generator is not None:
            out["generator"] = self.generator
        return out


@dataclass(frozen=True)
class FixtureManifest:
    """Full manifest of committed fixtures."""

    entries: tuple[FixtureEntry, ...]

    def by_id(self, fixture_id: str) -> FixtureEntry:
        """Return the entry with the given id or raise ``KeyError``."""
        for entry in self.entries:
            if entry.id == fixture_id:
                return entry
        raise KeyError(f"no fixture with id {fixture_id!r}")

    def by_axis(self, axis: str) -> tuple[FixtureEntry, ...]:
        """Return every fixture on the given axis."""
        return tuple(e for e in self.entries if e.axis == axis)


_MANIFEST_FILENAME = "manifest.json"


def _package_root() -> Path:
    """Absolute path to the ``fixtures/`` package directory."""
    return Path(__file__).resolve().parent


def load_manifest(path: Path | None = None) -> FixtureManifest:
    """Load the manifest from disk.

    Args:
        path: Optional override for testing. Defaults to the manifest
            committed alongside this module.

    Returns:
        A :class:`FixtureManifest` with one entry per declared fixture.
    """
    manifest_path = path if path is not None else _package_root() / _MANIFEST_FILENAME
    if not manifest_path.exists():
        # Empty manifest is valid — the harness has no fixtures yet.
        return FixtureManifest(entries=())

    raw = json.loads(manifest_path.read_text())
    entries = tuple(FixtureEntry.from_dict(e) for e in raw.get("fixtures", []))
    return FixtureManifest(entries=entries)


def resolve_fixture_path(entry: FixtureEntry) -> Path:
    """Return the absolute on-disk path for a fixture entry."""
    return _package_root() / entry.relative_path


def compute_sha256(path: Path) -> str:
    """Compute the hex-sha256 of the file at ``path``."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
