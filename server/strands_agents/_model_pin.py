"""Anti-drift model pinning utility.

Enforces the rule that production workers load **exactly** the model
weights the codebase committed to — no silent revision drift, no
"main branch", no LLM agent quietly swapping ``model_id`` between
runs.

How it works
------------

A :class:`ModelPin` is a frozen dataclass that locks three things:

* ``model_id`` — Hugging Face repo id, e.g. ``Lightricks/LTX-2.3``.
* ``revision`` — exact git commit SHA on that repo. Never a branch
  name, never ``main``, never a tag.
* ``required_files`` — mapping of *file path inside the snapshot* to
  the SHA256 the file is expected to hash to. The hashes are the LFS
  ``oid sha256`` values Hugging Face publishes for each safetensors
  file (verifiable via ``GET https://huggingface.co/api/models/<repo>
  /tree/<revision>``).

At engine startup, :func:`verify_pin` is called *before* any model is
loaded. It

1. Calls ``huggingface_hub.snapshot_download`` with ``revision`` set to
   the pinned commit SHA and ``allow_patterns`` set to the required
   files. This either pulls the exact bytes from HF (or
   ``HF_ENDPOINT`` mirror) into the local cache, or — if the cache
   already has a snapshot at that revision — returns the local
   snapshot dir without a network round trip.
2. Computes SHA256 of every required file on disk.
3. Compares each hash to the pinned ``required_files`` value.
4. Raises :class:`ModelPinMismatchError` on the *first* mismatch.

If verification passes, the function returns the snapshot directory
that the caller can hand to ``from_pretrained(...)`` — guaranteeing
the loaded weights are exactly the bytes the codebase pinned.

Anti-drift property
-------------------

The pin module fields are immutable (``frozen=True``). Any code
change that tries to alter the locked values shows up as a visible
diff in the PR. A unit test asserts the dataclass is frozen, so
attempting to mutate it at runtime fails loudly. A future LLM that
edits the pin without also updating the SHA256 hashes will either

* leave the hashes mismatched against the new revision → engine
  startup raises :class:`ModelPinMismatchError`, the worker dies,
  CI catches it; or
* update the hashes too → the diff shows the model swap explicitly,
  reviewers can reject it.

Either way, the model selection is no longer quietly swappable.

There is intentionally **no environment-variable override** for any
of the pinned fields. Operational knobs (device, dtype, attention
implementation, etc.) remain env-var configurable on the engine
itself; the model identity does not.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)


_HASH_CHUNK_BYTES = 1024 * 1024


class ModelPinMismatchError(RuntimeError):
    """Raised when a pinned model file's SHA256 does not match.

    Subclasses :class:`RuntimeError` so the engine startup path
    bubbles it up without needing a custom catch.
    """


@dataclass(frozen=True, slots=True)
class ModelPin:
    """Immutable model pin.

    Attributes:
        model_id: Hugging Face repo id (e.g. ``"Lightricks/LTX-2.3"``).
        revision: Exact git commit SHA on the repo. Branch names and
            tags are intentionally not supported — only a 40-char
            commit SHA pins the bytes.
        required_files: Mapping of file path inside the snapshot
            directory (forward slashes, no leading slash) to the
            expected SHA256 hex digest of that file. Every entry in
            this map is verified at startup.
        purpose: Short human-readable label used in log lines and
            error messages — e.g. ``"qwen3-tts"`` or ``"ltx-video"``.
    """

    model_id: str
    revision: str
    required_files: Mapping[str, str]
    purpose: str


def _hash_file_sha256(path: Path) -> str:
    """Compute the SHA256 hex digest of a file on disk.

    Streams the file in 1 MiB chunks so large safetensors files do
    not require materializing the whole file in memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _materialize_snapshot(pin: ModelPin) -> Path:
    """Resolve the local snapshot directory for the pin.

    Calls ``huggingface_hub.snapshot_download`` with the pinned
    revision and the pin's required files as the allow-list. If the
    files are already in the local HF cache at that revision, no
    network round trip happens.

    The import of ``huggingface_hub`` is local so the rest of this
    module remains importable in environments that don't have it
    (e.g., the CPU-only CI host that runs the unit tests with
    monkeypatched filesystem fixtures).
    """
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    snapshot_dir = snapshot_download(
        repo_id=pin.model_id,
        revision=pin.revision,
        allow_patterns=list(pin.required_files.keys()),
    )
    return Path(snapshot_dir)


def verify_pin(pin: ModelPin, *, snapshot_dir: Path | None = None) -> Path:
    """Verify that the locked model files hash to the pinned values.

    Args:
        pin: The :class:`ModelPin` to verify.
        snapshot_dir: Optional path to an already-materialized
            snapshot. If ``None`` (the default), the snapshot is
            resolved via ``huggingface_hub.snapshot_download`` at
            ``pin.revision``. Tests pass a synthetic directory here
            to avoid the HF round trip.

    Returns:
        The verified snapshot directory. Callers may pass this to
        ``from_pretrained`` to guarantee the weights they load are
        the verified bytes.

    Raises:
        ModelPinMismatchError: If a required file is missing or its
            SHA256 differs from the pinned value.
    """
    resolved = snapshot_dir if snapshot_dir is not None else _materialize_snapshot(pin)
    logger.info(
        "purpose=<%s>, model_id=<%s>, revision=<%s>, snapshot_dir=<%s> | verifying model pin",
        pin.purpose,
        pin.model_id,
        pin.revision,
        resolved,
    )
    for relative_path, expected_sha256 in pin.required_files.items():
        file_path = resolved / relative_path
        if not file_path.is_file():
            raise ModelPinMismatchError(
                f"model pin verification failed for purpose={pin.purpose!r}: "
                f"required file {relative_path!r} missing under {resolved}"
            )
        actual = _hash_file_sha256(file_path)
        if actual != expected_sha256:
            raise ModelPinMismatchError(
                f"model pin verification failed for purpose={pin.purpose!r}: "
                f"file={relative_path!r} actual_sha256={actual!r} "
                f"expected_sha256={expected_sha256!r} "
                f"(model_id={pin.model_id!r}, revision={pin.revision!r})"
            )
        logger.debug(
            "purpose=<%s>, file=<%s>, sha256=<%s> | model pin file verified",
            pin.purpose,
            relative_path,
            actual,
        )
    logger.info(
        "purpose=<%s>, model_id=<%s>, revision=<%s>, files=<%d> | model pin verified",
        pin.purpose,
        pin.model_id,
        pin.revision,
        len(pin.required_files),
    )
    return resolved


__all__ = [
    "ModelPin",
    "ModelPinMismatchError",
    "verify_pin",
]
