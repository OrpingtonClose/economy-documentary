"""Append-only event store. The single source of truth for the pipeline.

Every validated effect becomes an immutable EventRecord appended to the log.
OTIO is a read model rebuilt from these events.
Nothing is ever deleted or mutated.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field, SerializeAsAny, model_validator

from effects import Effect


_EFFECT_CLASSES: dict[str, type[Effect]] = {
    "UpdateScript": __import__("effects", fromlist=["UpdateScript"]).UpdateScript,
    "GenerateNarrationAudio": __import__("effects", fromlist=["GenerateNarrationAudio"]).GenerateNarrationAudio,
    "RenderVideoSegment": __import__("effects", fromlist=["RenderVideoSegment"]).RenderVideoSegment,
    "MergeIntoOTIO": __import__("effects", fromlist=["MergeIntoOTIO"]).MergeIntoOTIO,
    "JobQueued": __import__("effects", fromlist=["JobQueued"]).JobQueued,
    "JobStarted": __import__("effects", fromlist=["JobStarted"]).JobStarted,
    "JobCompleted": __import__("effects", fromlist=["JobCompleted"]).JobCompleted,
    "JobFailed": __import__("effects", fromlist=["JobFailed"]).JobFailed,
    "VMAllocated": __import__("effects", fromlist=["VMAllocated"]).VMAllocated,
    "VMDeallocated": __import__("effects", fromlist=["VMDeallocated"]).VMDeallocated,
    "VMProvisionFailed": __import__("effects", fromlist=["VMProvisionFailed"]).VMProvisionFailed,
    "QAPassed": __import__("effects", fromlist=["QAPassed"]).QAPassed,
    "QAFailed": __import__("effects", fromlist=["QAFailed"]).QAFailed,
    "JobRequeued": __import__("effects", fromlist=["JobRequeued"]).JobRequeued,
    "NoOp": __import__("effects", fromlist=["NoOp"]).NoOp,
}


class EventRecord(BaseModel):
    """A single immutable event in the log."""

    seq: int = Field(description="Monotonically increasing sequence number")
    effect: SerializeAsAny[Effect] = Field(description="The typed effect that caused this event")
    otio_hash_before: str = Field(description="OTIO hash before applying this effect")
    otio_hash_after: str = Field(default="", description="OTIO hash after applying this effect")
    validated: bool = Field(default=True, description="Whether this effect passed validation")
    rejected_reason: str = Field(default="", description="If rejected, why")

    @model_validator(mode="before")
    @classmethod
    def _parse_effect_subclass(cls, v: Any) -> Any:
        """Deserialize effect into its proper subclass based on effect_type."""
        if isinstance(v, dict) and "effect" in v:
            effect_data = v["effect"]
            if isinstance(effect_data, dict):
                effect_type = effect_data.get("effect_type", "NoOp")
                effect_cls = _EFFECT_CLASSES.get(effect_type, _EFFECT_CLASSES["NoOp"])
                v["effect"] = effect_cls(**effect_data)
        return v


class EventStore:
    """Append-only event log backed by a JSONL file.

    The event log is the single source of truth.
    OTIO is a materialized view rebuilt from events.
    """

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self._seq = self._last_seq()

    def _last_seq(self) -> int:
        """Read the last sequence number from the log."""
        if not os.path.exists(self.log_path):
            return 0
        last_seq = 0
        try:
            with open(self.log_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = EventRecord.model_validate_json(line)
                        last_seq = max(last_seq, record.seq)
                    except Exception:
                        continue
        except Exception:
            logger.exception("Event store read failed, starting fresh")
        return last_seq

    def append(self, effect: Effect, otio_hash_before: str) -> EventRecord:
        """Append a validated effect to the log. Returns the record."""
        self._seq += 1
        record = EventRecord(
            seq=self._seq,
            effect=effect,
            otio_hash_before=otio_hash_before,
        )
        with open(self.log_path, "a") as f:
            f.write(record.model_dump_json() + "\n")
        return record

    def read_all(self) -> list[EventRecord]:
        """Read all events from the log."""
        events: list[EventRecord] = []
        if not os.path.exists(self.log_path):
            return events
        with open(self.log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(EventRecord.model_validate_json(line))
                except Exception:
                    continue
        return events

    def count(self) -> int:
        """Return the total number of events in the log."""
        return self._seq
