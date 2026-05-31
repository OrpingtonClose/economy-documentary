> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Opznai  
  
  
  
*   
*   
*   
*   
*   
*   
*   
*   
*   
*   
*   
*   
*   
*   
  
  
  
  
##   
## ARCHITECTURE_V5.md  
## File  
**assess this architecture. make it complete or refine it**  
##   
I’ll treat the uploaded V5 doc as the source and focus on architectural completeness: invariants, failure modes, missing contracts, and implementation gaps rather than rewriting everything verbatim.  
## Thought for 4s  
##   
Strong foundation, but not complete yet. The biggest fixes are:  
1. Rename “no orchestrator” to “state-machine coordinator.” It still coordinates agents via state entry POSTs, so the principle should be “no LLM orchestrator,” not no orchestrator.   
2. Fix the SCRIPT → AUDIO_VIDEO guard. It currently uses _audio_reconciled, but audio cannot be reconciled before entering AUDIO_VIDEO. Replace with _script_complete.  
3. Resolve “no timeouts” contradiction. JobFailed.failure_category includes "timeout", while the architecture bans timeouts. Either remove it or clarify: “external worker/runtime timeout reported by engine/provider, never enforced by pipeline.”  
4. Add event invariants. Every effect needs run_id, effect_id, causation_id, correlation_id, producer, and preferably schema_version.  
5. Add idempotency. Agents, provisioner callbacks, and VM reports will duplicate. JobCompleted, MergeIntoOTIO, VMAllocated, etc. need deterministic IDs and replay-safe projection logic.  
6. Separate command vs event. QueueJob, ExecuteRawBash, and HumanInstruction are more command-like than event-like. Better names:  
    * JobQueued  
    * RawBashRequested  
    * HumanInstructionReceived  
7. Add unresolved/resolved failure lifecycle. ProductionFailed is append-only, but ASSEMBLY blocks forever because production_failures never clear. Add:  
    * ProductionFailureResolved  
    * JobFailureResolved  
    * ReconciliationFailureResolved  
8. Define job leasing. Pending jobs need ownership:  
    * JobLeased  
    * JobLeaseRenewed  
    * JobLeaseReleased  
    * JobCancelled  
9. Clarify projection purity. VMObserved says producer is VM Projection, but projections usually should not mutate the event log. Either allow “observer projections emit events” explicitly or move Vast polling into Provisioner.  
10. Add artifact contract. Local artifacts need immutable paths, checksums, media metadata, format, frame rate/sample rate, and retention policy.  
A tighter state machine:  
```

INIT
  → SCRIPT when PipelineStarted

SCRIPT
```
```


```
```
  → AUDIO_RECONCILE when script_complete
```
```


```
```
  → SCRIPT otherwise
```
```


```
```

AUDIO_RECONCILE
```
```


```
```
  → VIDEO_PRODUCTION when reconciliation_complete
```
```


```
```
  → SCRIPT when script_failure_unresolved
```
```


```
```
  → AUDIO_RECONCILE otherwise

VIDEO_PRODUCTION
```
```


```
```
  → ASSEMBLY when all_video_slots_filled and no_pending_jobs
```
```


```
```
  → SCRIPT when script_failure_unresolved
  → VIDEO_PRODUCTION otherwise
```
```


```
```

ASSEMBLY
  → DONE when final_mp4_exists and otio_valid and no_unresolved_failures
```
```


```
```
  → AUDIO_RECONCILE / VIDEO_PRODUCTION based on failure routing
```
```


```
```
  → ASSEMBLY otherwise


```
Most important implementation addition: a replay/idempotency test suite. For every effect type, replay the same event log twice and assert projections are identical. That will catch most architectural holes early.  
##   
##   
##   
  
##   
##   
##   
##   
##   
  
  
