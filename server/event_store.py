"""Append-only event store. The single source of truth for the pipeline.

Every validated effect becomes an immutable EventRecord appended to the log.
OTIO is a read model rebuilt from these events.
Nothing is ever deleted or mutated.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field

from effects import Effect


class EventRecord(BaseModel):
    """A single immutable event in the log."""

    seq: int = Field(description="Monotonically increasing sequence number")
    effect: Effect = Field(description="The typed effect that caused this event")
    otio_hash_before: str = Field(description="OTIO hash before applying this effect")
    otio_hash_after: str = Field(default="", description="OTIO hash after applying this effect")
    validated: bool = Field(default=True, description="Whether this effect passed validation")
    rejected_reason: str = Field(default="", description="If rejected, why")


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
            pass
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
