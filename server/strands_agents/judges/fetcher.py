"""Pull open-weight judge models from the pipeline's B2 bucket.

The abliterated Gemma 4 weights are the only set that can't be fetched
from the HuggingFace hub — they live privately under the pipeline's B2
bucket.  Qwen3.5-Omni and video-SALMONN 2 are available upstream but we
mirror them to B2 so provisioned Vast.ai VMs with restricted egress can
still bootstrap without going through HF.

This module is the *fetch side only*.  Uploading new judge weights to
B2 is a one-time human operation documented in
``docs/strands-migration/judges/PROVISIONING.md`` — we don't expose an
upload path here because pushing multi-hundred-GB weights belongs in an
administrator script, not the agent runtime.

The fetcher is designed to be resumable: partial downloads are kept
under ``<dest>.part`` and atomically renamed on success.  If a shard is
already on disk with the expected byte size, it's skipped.  Tests swap
the B2 client for a fake via the ``bucket_factory`` hook.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from strands_agents.judges.models import JudgeModelSpec

logger = logging.getLogger(__name__)


BucketFactory = Callable[[], Any]
"""Zero-arg factory returning a b2sdk-compatible bucket handle.

Kept pluggable so tests can inject a fake that records download calls
rather than hitting the network.  Production callers fall back to the
default factory that reuses the pipeline's existing B2 singleton.
"""


def _default_bucket_factory() -> Any:
    """Return the pipeline's shared B2 bucket handle.

    We import inside the function so modules that only need the catalog
    (e.g. docs generators, unit tests of the client layer) don't pay
    the ``b2sdk`` import cost.
    """

    from tools.b2_checkpoint import _get_bucket  # type: ignore[import-not-found]

    bucket = _get_bucket()
    if bucket is None:
        raise RuntimeError(
            "B2 is not configured — set B2_KEY_ID and B2_APPLICATION_KEY "
            "before fetching judge weights"
        )
    return bucket


def fetch_model_from_b2(
    spec: JudgeModelSpec,
    dest_root: Path | str,
    *,
    bucket_factory: Optional[BucketFactory] = None,
    files: Optional[Iterable[str]] = None,
    force: bool = False,
) -> list[Path]:
    """Download every checkpoint shard for ``spec`` into ``dest_root/<key>/``.

    Args:
        spec: Which judge model to pull.  Uses :attr:`spec.b2_prefix`
            and :attr:`spec.checkpoint_files` to build the B2 keys.
        dest_root: Local directory root.  The actual files land under
            ``dest_root/<spec.key>/<file_name>``.  Created if missing.
        bucket_factory: Optional override for the B2 bucket handle.
            Tests inject a fake here.
        files: Optional subset of :attr:`spec.checkpoint_files` to
            fetch.  Defaults to all.  Useful for pulling just the
            config/tokenizer to warm the VM disk before the big shards
            arrive.
        force: When True, re-download even if the local file exists
            and has the same size as the B2 object.

    Returns:
        List of absolute :class:`Path` objects for the downloaded
        files, in the order they were fetched.

    Raises:
        RuntimeError: If ``spec.b2_prefix`` is empty (model is not
            mirrored to B2) — callers fetch from the HF hub in that
            case.
        FileNotFoundError: If a requested file is missing in B2.  Raised
            after any earlier files have finished, so partial fetches
            still make progress.
    """

    if not spec.b2_prefix:
        raise RuntimeError(
            f"model {spec.key!r} has no B2 mirror (b2_prefix is empty); "
            "use the HuggingFace source instead"
        )

    target_files = tuple(files) if files is not None else spec.checkpoint_files
    if not target_files:
        return []

    factory = bucket_factory or _default_bucket_factory
    bucket = factory()

    dest_dir = Path(dest_root) / spec.key
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[Path] = []
    for file_name in target_files:
        key = f"{spec.b2_prefix.rstrip('/')}/{file_name}"
        dest = dest_dir / file_name

        if dest.exists() and not force:
            expected_size = _bucket_file_size(bucket, key)
            if expected_size is not None and dest.stat().st_size == expected_size:
                logger.info(
                    "model=<%s>, file=<%s>, size=<%d> | already on disk, skipping",
                    spec.key,
                    file_name,
                    expected_size,
                )
                downloaded.append(dest)
                continue
            logger.info(
                "model=<%s>, file=<%s>, local_size=<%d>, remote_size=<%s> | "
                "size mismatch, redownloading",
                spec.key,
                file_name,
                dest.stat().st_size,
                expected_size,
            )

        _download_atomic(bucket, key, dest)
        downloaded.append(dest)
        logger.info(
            "model=<%s>, file=<%s>, bytes=<%d> | judge weight downloaded",
            spec.key,
            file_name,
            dest.stat().st_size,
        )

    return downloaded


def _bucket_file_size(bucket: Any, key: str) -> Optional[int]:
    """Return the size of ``key`` in ``bucket`` or None if unavailable."""

    get_info = getattr(bucket, "get_file_info_by_name", None)
    if get_info is None:
        return None
    try:
        info = get_info(key)
    except Exception as exc:  # pragma: no cover — b2sdk raises different types
        logger.debug("file_info lookup failed for %s: %s", key, exc)
        return None
    size = getattr(info, "size", None)
    if size is None:
        size = getattr(info, "content_length", None)
    if isinstance(size, int):
        return size
    return None


def _download_atomic(bucket: Any, key: str, dest: Path) -> None:
    """Download ``key`` to ``dest.part`` then atomically rename to ``dest``.

    Atomicity matters because a worker can crash mid-download; if the
    next session found half a shard under the final name it would load
    garbage.  ``.part`` stays around for manual inspection when
    downloads fail, so admins can see how far the fetch got.
    """

    part = dest.with_suffix(dest.suffix + ".part")
    try:
        if part.exists():
            part.unlink()
        download_dest = getattr(bucket, "download_file_by_name", None)
        if download_dest is None:
            raise RuntimeError(
                "bucket handle lacks download_file_by_name; "
                "upgrade b2sdk or inject a compatible mock"
            )
        stream = download_dest(key)
        save_to = getattr(stream, "save_to", None) or getattr(stream, "save", None)
        if save_to is None:
            raise RuntimeError(
                "b2 DownloadedFile has neither save_to nor save; "
                "b2sdk major version changed?"
            )
        save_to(str(part))
    except Exception as exc:
        # Surface the original exception class to the caller so they
        # can distinguish "key missing" from "network failure" if the
        # underlying SDK throws typed errors.
        if part.exists():
            logger.warning(
                "key=<%s>, partial_size=<%d> | judge weight download failed, keeping .part",
                key,
                part.stat().st_size,
            )
        else:
            logger.warning("key=<%s> | judge weight download failed: %s", key, exc)
        raise

    shutil.move(str(part), str(dest))


def ensure_parent_dir(path: Path | str) -> Path:
    """Create ``path`` (if directory) or ``path.parent`` (if file) and return it.

    Utility kept in this module because judge provisioning scripts pull
    it to pre-create the cache hierarchy before kicking off the fetch.
    """

    p = Path(path)
    if p.suffix:
        p.parent.mkdir(parents=True, exist_ok=True)
        return p.parent
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_root_from_env(default: str = "/tmp/judge-cache") -> Path:
    """Return the configured judge-weights cache directory.

    Looks up ``JUDGE_CACHE_ROOT`` first, falling back to ``default``.
    The judge worker script reads the same env var so the fetcher and
    the serving code see the same path without plumbing arguments
    through.
    """

    root = os.environ.get("JUDGE_CACHE_ROOT", default)
    return Path(root)
