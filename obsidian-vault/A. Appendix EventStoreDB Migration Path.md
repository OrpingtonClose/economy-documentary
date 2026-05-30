---
{
  "title": "A. Appendix: EventStoreDB Migration Path",
  "section": "",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[05 - Event Store|Event Store]] | [[00 - Index|Index]] | [[06 - Projections|Projections]] ->

# A. Appendix: EventStoreDB Migration Path


This appendix documents the EventStoreDB interface that the SQLite `EventStore` class is designed to be swappable with. If deploying to Linux servers with Docker, swap `EventStore` for an `esdbclient`-based implementation.

### A.1 Why EventStoreDB for distributed deployments

| Feature | SQLite | EventStoreDB |
|---|---|---|
| Push subscriptions | Polling (1s) | Native `subscribe_to_stream()` |
| Deduplication | In-memory `_seen` set | Native server-side |
| Concurrent writes | `asyncio.Lock` per run | Native stream serialization |
| Replication | Manual file copy | Built-in cluster replication |
| Operational tooling | `cat`, `grep` | Web UI, projections, monitoring |
| Cross-machine | NFS/shared volume | TCP protocol |

### A.2 ESDB client interface

```python
from esdbclient import EventStoreDBClient, NewEvent, StreamState

client = EventStoreDBClient(uri="esdb://localhost:2113?tls=false")

async def append_effect_esdb(
    run_id: str, effect: Effect, causation_id: str = "", correlation_id: str = ""
) -> int:
    stream_name = f"run-{run_id}"
    event = NewEvent(
        type=effect.kind,
        data=effect.model_dump_json().encode(),
        metadata=json.dumps({
            "agent": effect.agent,
            "timestamp": effect.timestamp,
            "run_id": run_id,
            "causation_id": causation_id or str(effect.effect_id),
            "correlation_id": correlation_id or str(effect.effect_id),
        }).encode(),
        event_id=str(effect.effect_id),
    )
    recorded = await client.append_to_stream(
        stream_name=stream_name, events=[event], current_version=StreamState.ANY,
    )
    return recorded.next_expected_version

async def read_since_esdb(run_id: str, from_revision: int = 0) -> list[dict[str, Any]]:
    stream_name = f"run-{run_id}"
    events = await client.get_stream(stream_name, from_revision=from_revision)
    return [
        {"sequence": e.revision, "effect_id": e.event_id, "kind": e.type,
         "payload_json": e.data.decode(), "created_at": e.commit_position}
        for e in events
    ]
```

### A.3 Swapping the backend

```python
class EventStoreBackend(Protocol):
    """V7.1: Protocol for swappable event store backends.

    JSONL implementation uses sync I/O (fine for single-process).
    ESDB implementation would use async I/O.
    """
    def append(self, run_id: str, effect: Effect, otio_hash_before: str) -> Any: ...
    def read_all(self, run_id: str) -> list[Any]: ...
    def read_since(self, run_id: str, from_seq: int) -> list[Any]: ...

# JSONL (current)
store: EventStoreBackend = EventStore(log_dir="/tmp/events")

# EventStoreDB (future)
# store: EventStoreBackend = ESDBEventStore(uri="esdb://localhost:2113?tls=false")
```

---

