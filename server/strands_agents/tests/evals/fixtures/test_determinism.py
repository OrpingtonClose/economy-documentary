"""Determinism tests for the committed media-fixture corpus.

These tests enforce two things the rest of the eval harness depends on:

1. **Committed bytes match the manifest.** Every fixture listed in
   ``manifest.json`` must exist on disk and hash to the sha256 the
   manifest claims. Runs on every CI invocation — no external tools
   needed, just a hash over committed files.
2. **Generators are deterministic.** Re-running each generator against
   its declared spec must produce byte-identical output to what's on
   disk. Skipped on hosts that lack ``ffmpeg`` / ``espeak-ng``; when
   it does run, a drift is a hard error.

A failure on test 1 means someone hand-edited a fixture without
updating the manifest. A failure on test 2 means either (a) the
generator regressed or (b) an external tool version changed and our
generator needs pinning.

No fixture is accepted into the corpus unless both tests pass locally
and in CI.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from strands_agents.evals.fixtures.generators.registry import (
    FixtureDeclaration,
    all_declarations,
)
from strands_agents.evals.fixtures.generators.video import generate_video
from strands_agents.evals.fixtures.generators.audio import generate_audio
from strands_agents.evals.fixtures.manifest import (
    compute_sha256,
    load_manifest,
    resolve_fixture_path,
)


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _espeak_available() -> bool:
    # The audio generator hard-requires ``espeak-ng`` (see
    # ``generators/audio.py``). Matching ``espeak`` here would pass the
    # skipif and then crash the generator, which defeats the purpose
    # of the skip.
    return shutil.which("espeak-ng") is not None


@pytest.mark.parametrize(
    "entry",
    load_manifest().entries,
    ids=lambda e: e.id,
)
def test_committed_fixture_matches_manifest_sha256(entry) -> None:
    """Every fixture's on-disk bytes must hash to the manifest sha256."""
    path = resolve_fixture_path(entry)
    assert path.exists(), f"fixture {entry.id} missing at {path}"
    actual = compute_sha256(path)
    assert actual == entry.sha256, (
        f"sha256 drift for fixture {entry.id!r}: "
        f"manifest claims {entry.sha256}, disk has {actual}"
    )


def test_manifest_has_expected_fixture_ids() -> None:
    """Manifest must cover every declared fixture.

    Guards against a half-written registry update (declaration added,
    ``build_all`` not re-run). Symmetric: a fixture committed without
    a matching declaration is also a bug.
    """
    declared_ids = {decl.id for decl in all_declarations()}
    manifest_ids = {e.id for e in load_manifest().entries}
    missing = declared_ids - manifest_ids
    extra = manifest_ids - declared_ids
    assert not missing, f"declared but not in manifest: {sorted(missing)}"
    assert not extra, f"in manifest but not declared: {sorted(extra)}"


def _regen_for(
    decl: FixtureDeclaration, out_root: Path
) -> tuple[Path, str]:
    """Regenerate one fixture into ``out_root`` and return (path, sha256)."""
    out_path = out_root / decl.relative_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if decl.media == "video":
        assert decl.video_spec is not None
        return generate_video(decl.video_spec, out_path)
    if decl.media == "audio":
        assert decl.audio_spec is not None
        return generate_audio(decl.audio_spec, out_path)
    raise ValueError(f"unknown media kind {decl.media!r}")


@pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg not available on this host; skipping video regeneration",
)
@pytest.mark.parametrize(
    "decl",
    [d for d in all_declarations() if d.media == "video"],
    ids=lambda d: d.id,
)
def test_video_generator_reproduces_committed_sha256(
    decl: FixtureDeclaration, tmp_path: Path
) -> None:
    """Regenerate each video fixture and verify sha256 matches committed bytes.

    The generator spec is the only input. If the sha256 drifts, either
    the generator code changed without a matching manifest update, or
    the host's ffmpeg version is different enough to disturb the bytes.
    Either way we want a loud failure.
    """
    _, regenerated = _regen_for(decl, tmp_path)
    manifest_entry = load_manifest().by_id(decl.id)
    assert regenerated == manifest_entry.sha256, (
        f"video generator produced {regenerated} for {decl.id}, "
        f"manifest expects {manifest_entry.sha256}"
    )


@pytest.mark.skipif(
    not _espeak_available(),
    reason="espeak-ng not available on this host; skipping audio regeneration",
)
@pytest.mark.parametrize(
    "decl",
    [d for d in all_declarations() if d.media == "audio"],
    ids=lambda d: d.id,
)
def test_audio_generator_reproduces_committed_sha256(
    decl: FixtureDeclaration, tmp_path: Path
) -> None:
    """Regenerate each audio fixture and verify sha256 matches committed bytes."""
    _, regenerated = _regen_for(decl, tmp_path)
    manifest_entry = load_manifest().by_id(decl.id)
    assert regenerated == manifest_entry.sha256, (
        f"audio generator produced {regenerated} for {decl.id}, "
        f"manifest expects {manifest_entry.sha256}"
    )


def test_manifest_entries_have_nonempty_prompt_and_verdict() -> None:
    """Sanity: every entry has the fields a judge case needs.

    A fixture without a prompt or verdict can't drive a live-judge
    case, so shipping one is a latent bug.
    """
    for entry in load_manifest().entries:
        assert entry.prompt.strip(), f"{entry.id} has empty prompt"
        assert entry.expected_verdict in {"yes", "no", "reject"}, (
            f"{entry.id} has unexpected verdict {entry.expected_verdict!r}"
        )
        assert entry.axis.strip(), f"{entry.id} has empty axis"
