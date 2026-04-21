"""In-memory B2 object-store fake.

:class:`FakeB2` stands in for the real Backblaze B2 uploads the audio
tool and assembly tool perform. Two properties matter:

* **Upload returns a stable URL-shaped string** — the pipeline passes
  these URLs through manifests, state graphs, and approval payloads;
  they must be usable as dict keys and string-compared elsewhere.
* **Uploaded bytes are recoverable by URL** — the simulator can feed
  the "uploaded" audio back into a later pipeline stage (e.g. the
  visual SubAgent that reads back the narration B2 URL) without
  touching the network.

The URL scheme is ``fake-b2://<hexhash>/<basename>`` where ``hexhash``
is derived from the uploaded bytes. This makes it deterministic and
content-addressable so identical uploads collapse to one URL — matches
how real B2 object-addressing tends to surface in debugging.
"""

from __future__ import annotations

import hashlib
import os
import threading

from strands_agents.sim.recorder import CallRecord, Recorder


class FakeB2:
    """In-memory content-addressable B2 stand-in."""

    URL_SCHEME = "fake-b2"

    def __init__(self, *, recorder: Recorder | None = None) -> None:
        """Create an empty store.

        Args:
            recorder: Optional :class:`Recorder` for trajectory capture.
        """
        self._lock = threading.Lock()
        self._blobs: dict[str, bytes] = {}
        self._recorder = recorder

    # ------------------------------------------------------------------
    # Upload-side interface — matches the shape of every real B2 helper
    # the pipeline uses: take a local path, return a URL string.
    # ------------------------------------------------------------------

    def upload(self, local_path: str) -> str:
        """Read ``local_path`` and register its bytes under a fake URL.

        Args:
            local_path: Filesystem path to the file to upload. The
                file is read once at upload time; later mutations of
                the local file are not reflected in the store.

        Returns:
            A ``fake-b2://<hash>/<basename>`` URL.

        Raises:
            FileNotFoundError: If ``local_path`` does not exist. This
                mirrors the real helper, which also raises on missing
                files — silent success here would mask wiring bugs.
        """
        with open(local_path, "rb") as fh:
            blob = fh.read()
        url = self._upload_bytes(blob, basename=os.path.basename(local_path))
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="b2",
                    op="upload",
                    args=(local_path,),
                    result_summary=f"url={url} bytes={len(blob)}",
                )
            )
        return url

    def upload_bytes(self, blob: bytes, *, basename: str) -> str:
        """Convenience for callers that already hold bytes in memory.

        Args:
            blob: The raw bytes to store.
            basename: A human-friendly filename to stamp in the URL —
                does not affect content-addressing.
        """
        url = self._upload_bytes(blob, basename=basename)
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="b2",
                    op="upload_bytes",
                    kwargs={"basename": basename},
                    result_summary=f"url={url} bytes={len(blob)}",
                )
            )
        return url

    # ------------------------------------------------------------------
    # Read-back interface — useful for assertions and for chaining
    # fakes (e.g. FakeRenderer wants to know how long the narration
    # audio is; it reads the WAV back from FakeB2).
    # ------------------------------------------------------------------

    def get(self, url: str) -> bytes:
        """Return the bytes previously uploaded under ``url``.

        Raises:
            KeyError: If the URL is unknown.
        """
        with self._lock:
            return self._blobs[url]

    def __contains__(self, url: str) -> bool:
        with self._lock:
            return url in self._blobs

    def __len__(self) -> int:
        with self._lock:
            return len(self._blobs)

    def urls(self) -> list[str]:
        """Return a snapshot of every registered URL."""
        with self._lock:
            return list(self._blobs.keys())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _upload_bytes(self, blob: bytes, *, basename: str) -> str:
        digest = hashlib.sha256(blob).hexdigest()[:16]
        safe_basename = basename or "blob"
        url = f"{self.URL_SCHEME}://{digest}/{safe_basename}"
        with self._lock:
            self._blobs[url] = blob
        return url
