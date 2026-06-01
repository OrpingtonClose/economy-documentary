---
{
  "title": "Traceability and Observability",
  "section": "16",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[15 - File Structure|File Structure]] | [[00 - Index|Index]] | [[17 - pydantic Ecosystem Deep Audit V7.1 Addendum|pydantic Ecosystem Deep Audit (V7.1 Addendum)]] ->

# Traceability and Observability


V7 does not adopt OpenTelemetry, W3C Trace Context, or structured metrics. Traceability is achieved through minimal, deterministic mechanisms that require no additional infrastructure.

### 16.1 Minimal traceability contract

| Mechanism | Location | Purpose |
|---|---|---|
| `effect_id` | Every effect (UUIDv7) | Idempotency key; also serves as exact event identifier |
| `agent` field | Every effect | Identifies which component produced the text from which the effect was extracted |
| `timestamp` | Every effect | Epoch seconds; monotonic overall |
| `sequence` | SQLite event sequence number | Total order of events within the event store |

### 16.2 Operator observability

The operator traces pipeline activity by:

1. **Event inspection:** `GET /` on the **Global State Agent** (port 8000) returns the complete projection bundle including `latest_sequence`. The operator can also query the SQLite event store directly via `store.read_all()` to see the raw event history.
2. **Log grep:** Components write detailed logs to `/tmp/documentary-pipeline/agent_debug_{role}.log`. The operator can grep by `effect_id=` to yield the complete execution trace of a specific effect.
3. **Projection state:** `GET /` on the Global State Agent returns `otio`, `jobs`, `vms`, `state`, and `budget` projections. `GET /` on individual agents returns agent-specific health status only.

No dashboards, no metrics servers, no collectors. The event stream is the single source of truth; logs are secondary; projection state is available on demand via HTTP.

---

### 16.3 Logging Specification

All components log to **stdout** (captured by Docker or systemd). Logs are plain text, not structured JSON. The format is:

```
YYYY-MM-DD HH:MM:SS.mmm | LEVEL | COMPONENT | effect_id=... | message
```

**Log levels:**

| Level | Used when | Example |
|---|---|---|
| `INFO` | Normal operation, effect appended, agent activated | `INFO scenario_agent effect_id=def UpdateScript appended for slot A1:3:2` |
| `WARN` | Recoverable anomaly, retry, slow operation | `WARN provisioner VM 12345 health check failed, retrying` |
| `ERROR` | Unrecoverable failure, crash, validation error | `ERROR audio_agent effect_id=def Parser ValidationError: voice must be V1/V2/V3` |
| `DEBUG` | Detailed internals (disabled in production) | `DEBUG parser extracted 2 effects confidence=8` |

**What gets logged at each step:**

| Step | Logged by | Content | Level |
|---|---|---|---|
| Agent receives POST / | Agent handler | `notification_type`, agent role | INFO |
| Agent queries GSA | Agent handler | `GET /` response time, projection counts | DEBUG |
| Narrative built | Agent handler | Number of situations, total narrative tokens | DEBUG |
| LLM call starts | pydantic-deep | Model name, token budget | DEBUG |
| LLM call completes | pydantic-deep | Output tokens, duration_sec | INFO |
| Parser runs | Parser | Phase results, effects extracted, confidence | INFO |
| Effects appended | Handler | Effect kinds, stream name, sequence numbers | INFO |
| Turn completed | Handler | Effects extracted | INFO |
| GSA processes event | GSA | Event kind, sequence, projection update delta | DEBUG |
| GSA serves GET / | GSA | `latest_sequence`, response size | DEBUG |
| VM worker starts job | VM worker | `job_id`, `job_type`, GPU info | INFO |
| VM worker completes | VM worker | `job_id`, duration_sec, artifact size | INFO |

**Log retention:** 7 days via Docker log rotation (`max-size=100m`, `max-file=10`). Operators grep logs using `effect_id=` as indexed prefixes.

---

### 16.4 Metrics (Projection-Derived, Not External)

V7 does not run a metrics server (Prometheus, StatsD, etc.). All "metrics" are derived from projections and returned via `GET /` on the GSA. The operator or an external script queries the GSA periodically and computes rates.

**Metrics available from `GlobalStateResponse`:**

| Metric | Source field | Unit |
|---|---|---|
| Effects appended | `latest_sequence` | count (per run) |
| Pipeline phase | `state.current_phase` | categorical |
| Budget spent | `budget.spent_usd` | USD |
| Budget remaining | `budget.remaining_usd` | USD |
| Active VMs | `vms.active_count` | count |
| VM hourly cost | `vms.estimated_hourly_cost_usd` | USD/hr |
| Total slots | `otio.total_slots` | count |
| Dirty slots | `otio.dirty_slots` | count |
| Measured slots | `otio.measured_slots` | count |
| Delivered slots | `otio.delivered_slots` | count |
| Pending jobs | `len(jobs.jobs)` with `status="pending"` | count |
| Running jobs | `len(jobs.jobs)` with `status="running"` | count |
| Failed jobs | `len(jobs.jobs)` with `status="failed"` | count |
| Production failures | `len(jobs.production_failures)` | count |

**Rate computation (external script example):**

```python
import time

class PipelineMonitor:
    """Simple monitor that polls GSA and computes rates."""

    def __init__(self, gsa_url: str):
        self.gsa_url = gsa_url
        self.last_seq: int = 0
        self.last_ts: float = 0.0

    async def poll(self):
        resp = await httpx.get(f"{self.gsa_url}/")
        data = resp.json()

        seq = data["latest_sequence"]
        now = time.time()

        if self.last_ts > 0:
            delta_seq = seq - self.last_seq
            delta_t = now - self.last_ts
            effects_per_sec = delta_seq / delta_t if delta_t > 0 else 0
            print(f"{effects_per_sec:.2f} effects/sec, phase={data['state']['current_phase']}")

        self.last_seq = seq
        self.last_ts = now
```

This script is **not part of the pipeline**. It is an external operator tool. No metrics are pushed or scraped by the pipeline itself.

---

### 16.5 Alerting Rules (Human-Triggered)

V7 has **no automated alerting system** (no PagerDuty, no webhooks). Alerts are conditions that the operator detects via `GET /` or log grep. The operator is expected to poll the GSA or scan logs periodically.

| Condition | How to detect | Operator action |
|---|---|---|
| `PipelineAborted` | `state.current_phase == "aborted"` | Inspect logs, determine cause, decide whether to restart or fix |
| `AgentLoopDetected` | `state.recent_effects` shows duplicate kinds 5+ times | POST `HumanInstruction` to the looping agent with directive to stop |
| `BudgetExceeded` | `budget.exceeded == True` | POST `HumanInstruction` with `action="budget_override"` or abort |
| `VMProvisionFailed` × 3 | `jobs.production_failures` has 3+ entries with `failure_category="vm_provision"` | Check Vast.ai account balance, retry, or switch GPU type |
| `JobFailed` (retryable=False) | `jobs.jobs` has job with `status="failed"` and `retryable=False` | Inspect error message, POST `HumanInstruction` to requeue or abort |
| `JobQueuedLong` | `jobs.jobs` has pending job with `created_at` > 5 min ago | Check `vms.active_count` — if 0, Provisioner may be stuck; operator should inspect |
| `BlockAtMaxAttempts` | `jobs.block_attempts[slot_id] >= max_attempts` | POST `HumanInstruction` to accept mismatch or rewrite script |
| Agent not responding | `GET /` on agent returns error or timeout | Restart agent process, check logs |
| Event store disk full | DB append raises OSError | Free disk space, restart agents |

**Why no automated alerts:** The pipeline is designed for attended operation during active runs. A typical documentary run completes in 10–30 minutes. The operator is present and polling. Automated alerting adds infrastructure (webhook endpoints, notification services, retry logic) that the architecture deliberately avoids.

---

### 16.6 Distributed Tracing via Causation Chains

V7 does not use OpenTelemetry, Jaeger, or W3C Trace Context. Tracing is achieved through causation and correlation IDs embedded in event metadata (§5.1.1).

**How to trace a causal chain:**

```python
async def trace_chain(job_id: str, store: EventStore):
    """Return all events related to a job, ordered by sequence."""
    records = store.replay()
    chain = [
        r for r in records
        if getattr(r.effect, "job_id", None) == job_id
    ]
    return sorted(chain, key=lambda r: r.seq)
```

**Example: trace a job from QueueJob to JobCompleted:**

```
$ python -c "
from server.event_store import EventStore

store = EventStore('/tmp/documentary-pipeline')
records = store.replay()
for r in records:
    if getattr(r.effect, 'job_id', None) == 'job-123':
        print(f'{r.seq}: {r.effect.kind} (job_id={r.effect.job_id})')
"

Output:
3: QueueJob (job_id=job-123)
4: VMAllocated (job_id=job-123)
5: JobStarted (job_id=job-123)
6: JobCompleted (job_id=job-123)
7: AudioMeasured (job_id=job-123)
```

**HTTP tracing:** The `X-Effect-ID` header is present on effect append responses, carrying the `effect_id` for correlation. This allows correlating HTTP traffic with event stream data:

```
Audio Agent completes turn; Video Agent polls GSA independently
  Headers:
    Content-Type: application/json
    X-Effect-ID: 0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b  # the ReconciliationComplete appended this turn
```

**Why this is sufficient:** A documentary pipeline has <10 agents and <2000 events per run. The operator can trace any issue by grepping logs and replaying the event stream. Distributed tracing infrastructure (spans, collectors, backends) is overkill for this scale and adds operational complexity.

---

