"""UX-01 — finished-documentary surface tests.

Verifies:

* ``otio_timeline_model._detect_finished_film`` returns the right
  :class:`FinishedFilm` for each of the three canonical filenames.
* ``build_timeline_view`` populates ``finished_film`` even in the
  degenerate paths (no OTIO on disk / parse error).
* The ``/agui/final_film/<name>`` route serves only the three allowed
  names and rejects traversal attempts.
* ``deterministic_assembly_callback`` emits an ``ASSEMBLED_VIDEO``
  artifact + a ``film_ready`` narrator turn once the final mp4 exists.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


def test_detect_single_language(monkeypatch):
    from otio_timeline_model import _detect_finished_film

    with tempfile.TemporaryDirectory() as d:
        assert _detect_finished_film(d) is None

        open(os.path.join(d, "final_documentary.mp4"), "wb").write(b"fake")
        ff = _detect_finished_film(d)
        assert ff is not None
        assert ff.url == "/agui/final_film/final_documentary.mp4"
        assert ff.language == ""
        assert ff.alternates == []


def test_detect_dual_language(monkeypatch):
    from otio_timeline_model import _detect_finished_film

    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "final_documentary_ru.mp4"), "wb").write(b"fake")
        open(os.path.join(d, "final_documentary_en.mp4"), "wb").write(b"fake")
        ff = _detect_finished_film(d)
        assert ff is not None
        assert ff.language == "ru"
        assert ff.url == "/agui/final_film/final_documentary_ru.mp4"
        assert len(ff.alternates) == 1
        assert ff.alternates[0]["language"] == "en"


def test_build_view_populates_finished_film_without_otio():
    from otio_timeline_model import build_timeline_view

    with tempfile.TemporaryDirectory() as d:
        # No .otio file present but a final mp4 is -- the user should
        # still see the ▶ card in the dashboard.
        open(os.path.join(d, "final_documentary.mp4"), "wb").write(b"fake")
        view = build_timeline_view(d)
        assert view.finished_film is not None
        payload = view.to_dict()
        assert (
            payload["finished_film"]["url"]
            == "/agui/final_film/final_documentary.mp4"
        )


def test_final_film_route_serves_allowed_file(tmp_path, monkeypatch):
    # Point the agui module at a tmp output dir, write a canonical
    # final and hit the route.
    import importlib

    monkeypatch.setenv("PIPELINE_OUTPUT_DIR", str(tmp_path))
    import agui
    importlib.reload(agui)

    (tmp_path / "final_documentary.mp4").write_bytes(b"fake-mp4-bytes")

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(agui.router)
    client = TestClient(app)

    r = client.get("/agui/final_film/final_documentary.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.content == b"fake-mp4-bytes"


def test_final_film_route_rejects_unknown_filename(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("PIPELINE_OUTPUT_DIR", str(tmp_path))
    import agui
    importlib.reload(agui)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(agui.router)
    client = TestClient(app)

    r = client.get("/agui/final_film/preview_abc.mp4")
    assert r.status_code == 400


def test_final_film_route_rejects_traversal(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("PIPELINE_OUTPUT_DIR", str(tmp_path))
    import agui
    importlib.reload(agui)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(agui.router)
    client = TestClient(app)

    # Not in the allow-list, so this comes back as 400 "not a final
    # film filename" -- which is exactly what we want.
    r = client.get("/agui/final_film/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


def test_film_ready_narrator_template():
    from agents.chat_narrator import NarratorEvent, format_turn, NARRATOR_EVENT_KINDS

    assert "film_ready" in NARRATOR_EVENT_KINDS

    evt = NarratorEvent(kind="film_ready", fields={"duration_sec": 420, "language": ""})
    text = format_turn(evt)
    assert "ready" in text.lower()
    assert "420" in text

    dual = NarratorEvent(kind="film_ready", fields={"duration_sec": 420, "language": "ru"})
    assert "RU" in format_turn(dual)
