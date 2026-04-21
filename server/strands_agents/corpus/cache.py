"""Content-addressed local cache for corpus artifacts.

Artifact bytes are stored under ``<root>/<sha256[:2]>/<sha256>`` so the
directory tree stays wide-but-shallow even when the corpus grows.  The
cache never evicts — corpus artifacts are small and immutable, and the
evals need determinism.  Operators who want to reclaim disk can ``rm
-rf`` the root manually.

The cache is process-safe but not strictly atomic: two callers writing
the same sha256 will both compute the same bytes (by definition of
content addressing), so the last-writer-wins behaviour is safe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default location for the corpus cache.  Override with
# ``STRANDS_CORPUS_CACHE`` env var for CI or sandboxed runs.  We don't
# use ``/tmp`` — the parent skill explicitly calls it out as wiped
# across restarts and excluded from VM snapshots, which would defeat
# the point of caching.
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "economy-documentary" / "corpus"


def resolve_cache_root(override: Optional[Path | str] = None) -> Path:
    """Return the cache root directory, creating it if necessary.

    Args:
        override: Explicit root to use.  Takes precedence over the env
            var and the default.

    Returns:
        Absolute :class:`Path` to the cache root.
    """
    if override is not None:
        root = Path(override)
    elif "STRANDS_CORPUS_CACHE" in os.environ:
        root = Path(os.environ["STRANDS_CORPUS_CACHE"])
    else:
        root = DEFAULT_CACHE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def path_for(root: Path, sha256: str) -> Path:
    """Return the cache path where the blob for ``sha256`` lives.

    Uses the first two hex chars as a shard prefix so ``ls`` on the
    root stays manageable even with thousands of artifacts.
    """
    if len(sha256) != 64:
        raise ValueError(f"invalid sha256=<{sha256}> (expected 64 hex chars)")
    return root / sha256[:2] / sha256


def compute_sha256(path: Path | str, *, chunk_size: int = 1 << 16) -> str:
    """Return the hex sha256 digest of the file at ``path``.

    The chunked read keeps memory flat even on multi-GB artifacts.
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def verify_bytes(path: Path, expected_sha256: str) -> bool:
    """Return True iff the file at ``path`` hashes to ``expected_sha256``."""
    if not path.exists():
        return False
    actual = compute_sha256(path)
    if actual == expected_sha256:
        return True
    logger.warning(
        "path=<%s>, expected=<%s>, actual=<%s> | sha256 mismatch",
        path, expected_sha256, actual,
    )
    return False


def store(
    root: Path,
    sha256: str,
    source: Path,
    *,
    copy: bool = True,
) -> Path:
    """Place ``source`` into the cache at the canonical path for ``sha256``.

    Verifies the hash before committing.  Uses a temp file in the same
    directory so the move is atomic on POSIX (no partial writes visible
    to other processes).

    Args:
        root: Cache root directory.
        sha256: Expected digest of the file bytes.
        source: Path to the file to ingest.
        copy: If True (default), copy the source.  If False, move it —
            destructive on ``source`` but avoids doubling the disk
            footprint during large imports.

    Returns:
        Path to the committed cache entry.

    Raises:
        ValueError: If the source bytes don't match ``sha256``.
    """
    if not verify_bytes(source, sha256):
        actual = compute_sha256(source) if source.exists() else "<missing>"
        raise ValueError(
            f"source=<{source}> sha256 mismatch: "
            f"expected=<{sha256}>, actual=<{actual}>"
        )

    target = path_for(root, sha256)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and verify_bytes(target, sha256):
        logger.debug(
            "sha256=<%s> | cache hit during store, skipping",
            sha256,
        )
        return target

    # Target is missing OR corrupted; fall through to rewrite it.  A
    # stale corrupted entry would otherwise permanently poison the cache
    # (resolve()'s re-fetch would land here, short-circuit, and the
    # verify at the end of resolve() would fail again — no self-healing).

    with tempfile.NamedTemporaryFile(
        dir=target.parent, delete=False, prefix=".corpus.", suffix=".tmp",
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        if copy:
            shutil.copy2(source, tmp_path)
        else:
            shutil.move(str(source), str(tmp_path))
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup — the temp name is unique per call.
        tmp_path.unlink(missing_ok=True)
        raise

    logger.info(
        "sha256=<%s>, path=<%s> | stored artifact in cache",
        sha256, target,
    )
    return target
