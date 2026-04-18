"""Tests for server/tools/b2_checkpoint.py bucket provisioning (#108).

Per-run B2 buckets must be created as ``allPublic`` so downstream
consumers (frontend, QA dashboard, human reviewers) can fetch artefacts
by URL.  These tests mock the b2sdk API surface and assert the create
call pins ``bucket_type="allPublic"``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


class _FakeBucket:
    def __init__(self, name: str, type_: str = "allPublic"):
        self.name = name
        self.type_ = type_


class _NonExistentBucket(Exception):
    pass


class _FakeB2Api:
    """Minimal b2sdk.v2.B2Api surface for the bucket-create code path."""

    def __init__(self, existing: dict[str, _FakeBucket] | None = None):
        self.existing = existing or {}
        self.create_calls: list[tuple[str, dict]] = []

    def get_bucket_by_name(self, name: str) -> _FakeBucket:
        if name not in self.existing:
            raise _NonExistentBucket(f"no such bucket: {name}")
        return self.existing[name]

    def create_bucket(self, name: str, bucket_type: str, **kwargs) -> _FakeBucket:
        self.create_calls.append((name, {"bucket_type": bucket_type, **kwargs}))
        bucket = _FakeBucket(name, type_=bucket_type)
        self.existing[name] = bucket
        return bucket


def test_ensure_public_bucket_creates_allpublic_when_missing():
    from tools.b2_checkpoint import B2_BUCKET_TYPE_PUBLIC, _ensure_public_bucket

    api = _FakeB2Api(existing={})
    bucket = _ensure_public_bucket(api, "doc-run-abc123")

    assert len(api.create_calls) == 1
    name, kwargs = api.create_calls[0]
    assert name == "doc-run-abc123"
    assert kwargs["bucket_type"] == "allPublic"
    assert kwargs["bucket_type"] == B2_BUCKET_TYPE_PUBLIC
    assert bucket.type_ == "allPublic"


def test_ensure_public_bucket_reuses_existing_public_bucket():
    from tools.b2_checkpoint import _ensure_public_bucket

    existing = _FakeBucket("doc-run-existing", type_="allPublic")
    api = _FakeB2Api(existing={"doc-run-existing": existing})

    result = _ensure_public_bucket(api, "doc-run-existing")

    assert result is existing
    assert api.create_calls == []


def test_ensure_public_bucket_warns_on_private_existing(caplog):
    """An existing private bucket must NOT be silently mutated — just
    warn loudly so the operator notices the downstream 401s."""
    from tools.b2_checkpoint import _ensure_public_bucket

    existing = _FakeBucket("doc-run-private", type_="allPrivate")
    api = _FakeB2Api(existing={"doc-run-private": existing})

    with caplog.at_level("WARNING"):
        result = _ensure_public_bucket(api, "doc-run-private")

    assert result is existing
    assert api.create_calls == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("allPrivate" in r.getMessage() for r in warnings)
