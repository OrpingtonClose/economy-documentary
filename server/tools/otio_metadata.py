"""
Stateless file-based pipeline metadata operations for OTIO timelines.

Every function takes ``timeline_path`` as a parameter — no module globals.
All reads and writes go through the OTIO file on disk via
:func:`tools.otio_file_ops.otio_read` and
:func:`tools.otio_file_ops.otio_read_modify_write`.

Metadata is stored under ``timeline.metadata["documentary"]``, with
provenance tracked under ``timeline.metadata["documentary"]["_provenance"]``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from tools.otio_file_ops import otio_read, otio_read_modify_write


def _to_native(value: Any) -> Any:
    """Convert OTIO container types to native Python types.

    OTIO's AnyDictionary and AnyVector survive copy.deepcopy, so we
    need explicit conversion for isinstance checks to work correctly.
    We check for mapping/sequence duck-typing rather than exact types.
    """
    # OTIO AnyDictionary — has .items()
    if hasattr(value, 'items') and hasattr(value, 'get'):
        return {k: _to_native(v) for k, v in value.items()}
    # OTIO AnyVector — iterable but not a string/bytes
    if hasattr(value, '__iter__') and not isinstance(value, (str, bytes)):
        return [_to_native(item) for item in value]
    return value


def read_pipeline_metadata(
    timeline_path: str,
    key: str,
    default: Any = None,
) -> Any:
    """Read a single key from the documentary metadata namespace.

    Reads ``timeline.metadata["documentary"][key]`` from the OTIO file on
    disk.  Returns *default* when the key is missing or when the
    ``"documentary"`` namespace does not exist yet.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.
        key: Metadata key to look up inside the ``"documentary"`` namespace.
        default: Value returned when the key is absent (defaults to ``None``).

    Returns:
        The stored value, or *default* if the key does not exist.
    """
    timeline = otio_read(timeline_path)
    doc = timeline.metadata.get("documentary", {})
    return _to_native(doc.get(key, default))


def write_pipeline_metadata(
    timeline_path: str,
    key: str,
    value: Any,
    provenance: dict | None = None,
) -> str:
    """Write a key/value pair into the documentary metadata namespace.

    Persists ``timeline.metadata["documentary"][key] = value`` via
    :func:`tools.otio_file_ops.otio_read_modify_write`.  When *provenance*
    is provided it is stored under
    ``timeline.metadata["documentary"]["_provenance"][key]``.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.
        key: Metadata key to write.
        value: Value to store.  Must be JSON-serialisable if the OTIO
            adapter requires it.
        provenance: Optional dict describing the origin of this metadata
            entry (e.g. ``{"stage": "audio", "tool": "whisperx"}``).

    Returns:
        JSON string ``{"written": true, "key": <key>}`` confirming the write.
    """

    def _mutate(timeline: Any) -> None:
        timeline.metadata.setdefault("documentary", {})
        timeline.metadata["documentary"][key] = value
        if provenance is not None:
            timeline.metadata["documentary"].setdefault("_provenance", {})[key] = provenance

    otio_read_modify_write(timeline_path, _mutate)
    return json.dumps({"written": True, "key": key})


def metadata_key_exists(timeline_path: str, key: str) -> bool:
    """Check whether a key exists in the documentary metadata namespace.

    This is cheaper than :func:`read_pipeline_metadata` when you only need
    to know *whether* a key is present — it avoids deserialising the value.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.
        key: Metadata key to check.

    Returns:
        ``True`` if the key is present, ``False`` otherwise.
    """
    timeline = otio_read(timeline_path)
    doc = timeline.metadata.get("documentary", {})
    return key in doc
