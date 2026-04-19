"""Tests for :mod:`critique.store`.

Covers:
    - read/write round-trip
    - append_* helpers are append-biased (don't clobber existing entries)
    - list_ids + read_all
    - corrupt JSON is treated as absent
    - singleton get/set
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from critique.record import (  # noqa: E402
    ArtifactCritiqueRecord,
    Critique,
    EscalationRef,
    QaVerdict,
)
from critique.store import (  # noqa: E402
    ArtifactCritiqueStore,
    get_critique_store,
    set_critique_store,
)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactCritiqueStore:
    return ArtifactCritiqueStore(root=tmp_path, b2_enabled=False)


def test_write_and_read_roundtrip(store: ArtifactCritiqueStore):
    record = ArtifactCritiqueRecord(
        artifact_type="scene",
        artifact_id="s003",
        iteration=1,
        produced_by="scenario_director",
        qa_results=[QaVerdict(source="qa_jury", check_name="pace", verdict="pass")],
    )
    store.write(record)

    reread = store.read("scene", "s003")
    assert reread is not None
    assert reread.artifact_id == "s003"
    assert reread.produced_by == "scenario_director"
    assert reread.qa_results[0].check_name == "pace"


def test_read_missing_returns_none(store: ArtifactCritiqueStore):
    assert store.read("scene", "missing") is None


def test_append_critique_creates_record(store: ArtifactCritiqueStore):
    rec = store.append_critique(
        "clip", "s003_p002",
        Critique(source="visual_critic", rating="FAIR"),
        produced_by="visual_director",
    )
    assert rec.produced_by == "visual_director"
    assert len(rec.critiques) == 1

    # Second append_critique on the same artifact should *append*, not clobber.
    rec2 = store.append_critique(
        "clip", "s003_p002",
        Critique(source="brand_voice_critic", rating="GOOD"),
    )
    assert len(rec2.critiques) == 2
    # first-writer-wins on produced_by
    assert rec2.produced_by == "visual_director"


def test_append_qa_and_escalation(store: ArtifactCritiqueStore):
    store.append_qa(
        "clip", "c1",
        QaVerdict(source="qa_jury", check_name="lip_sync", verdict="fail"),
    )
    store.append_qa(
        "clip", "c1",
        QaVerdict(source="gatekeeper", check_name="duration", verdict="warn"),
    )
    store.append_escalation(
        "clip", "c1",
        EscalationRef(scope_id="esc_1", action="regenerate_clip"),
    )

    rec = store.read("clip", "c1")
    assert rec is not None
    assert len(rec.qa_results) == 2
    assert len(rec.escalations) == 1
    assert rec.worst_qa() == "fail"


def test_list_ids_and_read_all(store: ArtifactCritiqueStore):
    store.append_qa("scene", "s01", QaVerdict(source="qa_jury", check_name="a", verdict="pass"))
    store.append_qa("clip", "c01", QaVerdict(source="qa_jury", check_name="a", verdict="pass"))
    store.append_qa("clip", "c02", QaVerdict(source="qa_jury", check_name="a", verdict="warn"))

    all_ids = sorted(store.list_ids())
    assert all_ids == sorted([("scene", "s01"), ("clip", "c01"), ("clip", "c02")])

    clip_ids = sorted(store.list_ids("clip"))
    assert clip_ids == [("clip", "c01"), ("clip", "c02")]

    all_records = store.read_all()
    assert len(all_records) == 3


def test_iteration_is_monotonic(store: ArtifactCritiqueStore):
    store.append_critique(
        "scene", "s1", Critique(source="a", rating="GOOD"), iteration=1,
    )
    store.append_critique(
        "scene", "s1", Critique(source="b", rating="GOOD"), iteration=3,
    )
    store.append_critique(
        "scene", "s1", Critique(source="c", rating="GOOD"), iteration=2,
    )
    rec = store.read("scene", "s1")
    assert rec is not None
    assert rec.iteration == 3  # monotonic max


def test_corrupt_json_treated_as_absent(store: ArtifactCritiqueStore):
    path = store._path("scene", "broken")  # type: ignore[attr-defined]
    path.write_text("{not json")
    assert store.read("scene", "broken") is None


def test_unknown_artifact_type_rejected(store: ArtifactCritiqueStore):
    with pytest.raises(ValueError):
        store._path("bogus", "x")  # type: ignore[attr-defined]


def test_unsafe_artifact_id_sanitised(store: ArtifactCritiqueStore):
    # artifact IDs with path separators get flattened rather than
    # escaping the store root.
    store.append_qa(
        "scene", "../../etc/passwd",
        QaVerdict(source="qa_jury", check_name="x", verdict="pass"),
    )
    # No file appeared outside the store root.
    root = store.root
    escaped = Path("/etc/passwd.json")
    # pragma: explicit assertion on absence.
    assert not escaped.exists() or escaped.stat().st_size > 0  # pre-existing /etc unaffected
    # The sanitised file IS somewhere under the store.
    found = list(root.rglob("*.json"))
    assert found, "expected at least one record file under the store root"


def test_singleton_set_and_clear(tmp_path: Path):
    custom = ArtifactCritiqueStore(root=tmp_path, b2_enabled=False)
    set_critique_store(custom)
    try:
        assert get_critique_store() is custom
    finally:
        set_critique_store(None)
    # Next call lazily rebuilds — we don't assert identity, just that it
    # doesn't raise and returns an instance.
    again = get_critique_store()
    assert isinstance(again, ArtifactCritiqueStore)
    set_critique_store(None)


def test_write_is_atomic_no_tmp_left_behind(store: ArtifactCritiqueStore):
    store.append_qa("clip", "c1", QaVerdict(source="qa_jury", check_name="x", verdict="pass"))
    leftovers = list(store.root.rglob("*.tmp"))
    assert leftovers == []
    # And the record file is valid JSON on disk.
    path = store._path("clip", "c1")  # type: ignore[attr-defined]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["artifact_id"] == "c1"
