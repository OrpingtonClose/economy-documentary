---
{
  "title": "Security Model",
  "section": "13",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[12 - Data Flows|Data Flows]] | [[00 - Index|Index]] | [[14 - Configuration|Configuration]] ->

# Security Model


### 13.1 bash_command Security

**Threat.** `bash_command` grants agents arbitrary shell access on worker VMs
and the control plane. A compromised or misdirected agent can execute destructive
commands or exfiltrate data.

**V7.1 architectural decision:** There is no per-command approval flow. The agent
calls `bash_command` directly; it executes immediately. Security is enforced at
the infrastructure level, not by gating individual commands.

**Defense layers:**

1. **VM isolation.** GPU workers are ephemeral. Each VM is created from a golden
   image, executes one stage of work, and is destroyed. Root disk is read-only;
   no writable overlay persists. A compromised VM is destroyed and replaced.

2. **Network egress restriction.** Worker VMs can only connect to the control
   plane host. All other egress is blocked at the hypervisor firewall. Even if
   an agent runs `curl` to an external site, the packet is dropped.

3. **Control plane sandbox.** Control plane agents (Scenario, Audio, Video,
   Assembly, Provisioner) run in a restricted environment with limited filesystem
   access. They cannot read operator credentials, access other runs' data, or
   modify system configuration.

4. **No secrets on workers.** VM workers have no API keys, no credentials, no
   tokens. They receive work descriptions via HTTP and return results via HTTP.
   All secrets live on the control plane only.

**Operator override.** The human operator can POST `HumanInstruction` to any
agent at any time. If an agent is misbehaving, the operator can instruct it to
stop, destroy its VMs, or abort the pipeline. This is the escape hatch — human
judgment, not automated allowlists.

```python
class VMIsolationConfig(BaseModel):
    """Security parameters for ephemeral GPU worker VMs.

    V7.1: No JWT, no B2 credentials, no checkpointing. VM destruction is
    operator-driven or triggered by the Provisioner observing stage completion.
    """
    # jwt_ttl_seconds REMOVED — no secrets on workers
    # destroy_after_stage_seconds REMOVED — no timeouts (Principle 4)
    allowed_egress_hosts: List[str] = Field(
        default_factory=lambda: ["coordinator.internal"]
    )
```

### 13.2 Budget Enforcement

**Threat.** LLM API calls, GPU rental, and storage accumulate cost without bound. A runaway pipeline can consume hundreds of dollars in minutes.

**Defense.** Every pipeline run carries a monotonically-increasing cost accumulator checked against a per-run budget cap. Default: $10.00 USD, configurable per-run via `budget_usd`. The accumulator tracks LLM tokens, GPU rental (per-second), and egress bandwidth (per-GB).

```python
class BudgetLedger(BaseModel):
    """Cumulative spend against a per-run budget ceiling."""
    budget_usd: float = Field(default=10.0, ge=0.01, le=1000.0)
    spent_llm_usd: float = Field(default=0.0)
    spent_gpu_usd: float = Field(default=0.0)
    spent_egress_usd: float = Field(default=0.0)

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - (
            self.spent_llm_usd + self.spent_gpu_usd + self.spent_egress_usd
        )

    def check(self, next_charge_usd: float) -> bool:
        return (self.remaining_usd - next_charge_usd) >= 0.0
```

**Escape hatch.** If a projected charge exceeds remaining budget, the parser extracts `PipelineAborted` with `reason=budget_exceeded` and a final ledger. All non-committed GPU instances are destroyed immediately. Partial outputs are retained for inspection.

### 13.3 Agent Loop Detection

**Threat.** An agent may enter an infinite loop: repeatedly calling the same tool with identical arguments, or cycling through strategies without progress.

**Defense.** Dual detection runs against every agent's turn history:

1. **Duplicate-effects detection.** Hashes observable side effects (files written, API calls, VMs launched) after each turn. Same hash twice within the window fires the detector.
2. **No-progress detection.** If the task-state score (completed checklist items) does not increase for `N` consecutive turns, the detector fires. Default `N=5`, configurable per agent type.

| Detector | Signal | Threshold | Action |
|---|---|---|---|
| Duplicate effects | Identical side-effect hash | 2 occurrences | `LoopDetected` → `ClarificationRequest` |
| No progress | Task-state delta = 0 | `N` turns (default 5) | `LoopDetected` → `ClarificationRequest` |

**Escape hatch.** Either trigger pauses the agent and surfaces a `ClarificationRequest` with the last `N` turns of context. The operator may resume with guidance, terminate the agent, or reassign.

```python
class LoopDetectorConfig(BaseModel):
    """Per-agent loop detection parameters."""
    progress_threshold_turns: int = Field(default=5, ge=2, le=20)
    effect_dedup_window: int = Field(default=10, ge=2, le=50)
    enabled_detectors: List[Literal["duplicate_effects", "no_progress"]] = Field(
        default_factory=lambda: ["duplicate_effects", "no_progress"]
    )
```

### 13.4 VM Isolation

**Threat.** GPU worker VMs execute arbitrary code. A compromised VM could exfiltrate secrets, persist malware, or attack the control plane host.

**Defense.** Two isolation layers:

1. **Ephemeral lifecycle.** VMs are created per pipeline stage and destroyed by the Provisioner agent when stage completion is observed via GSA projections. Root disks are provisioned from a golden image; no writable overlay persists.
2. **Network egress restriction.** Outbound connections are limited to the control plane host. All other egress is blocked at the hypervisor firewall.

**V7.1 architectural decision:** No secrets on workers. Artifacts are streamed
back to the Provisioner via HTTP (the same POST / response that reports job
completion). No B2 bucket, no JWT, no credential injection complexity. If external
storage is needed later, the operator injects credentials via the cloud provider's
startup script or file-injection mechanism — the VM reads them from a file, not
from environment variables. No token-vending service.

```

**Escape hatch.** Anomalous behavior (failed health check, unexpected process, unauthorized connection) triggers immediate VM destruction and stage retry on a fresh instance. Anomaly events are logged to a security audit stream.

---

