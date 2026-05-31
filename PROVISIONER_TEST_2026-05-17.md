> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Provisioner Integration Test — 2026-05-17

## Objective
Test the job queue + provisioner loop + Vast.ai VM + B2 pipeline outside the main graph, to validate the architecture before a full pipeline run.

## Test Results

### 1. /cheat Scan — Modified Files

All files modified in this session were scanned for:
- Timeouts (`urlopen(timeout=...)`, `subprocess(timeout=...)`)
- Stubs (`NotImplementedError`, `return "mock"`)
- Swallowed exceptions (`except Exception: pass` without `notify_maintainer`)
- Fixed polling loops (`for ... in range(...) + time.sleep`)
- Algorithmic retry (`for ... in range(...) + try:`)

**Result: 0 violations in modified files.**

Previously-fixed violations:
| Violation | File | Fix |
|-----------|------|-----|
| `probe_clip` stub | `tools/video_tools.py` | ffprobe subprocess |
| `mux_audio_video` stub | `tools/assembly_tools.py` | ffmpeg subprocess |
| `normalize_audio_loudness` stub | `tools/assembly_tools.py` | ffmpeg loudnorm two-pass |
| `timeout=15` Brave/Exa | `research_tools.py` | Removed |
| `timeout=10` worker health | `vm_registry_tools.py` | Removed |
| Domain/Provisioner mixing | `graph_pipeline.py` | Stripped infra tools from audio/video agents |
| Fixed polling loops | `vast_provisioning.py` | Removed `range(60)` + `time.sleep(10)` |
| Algorithmic retry | `graph_pipeline.py` | Removed `for attempt in range(max_retries)` |

### 2. Vast.ai Account Status — BLOCKED

```
$ vastai show user --raw
Credit: 0
Email: orpington.close@gmail.com
```

**Finding: Account has $0 credit. VM provisioning is impossible.**

```
$ vastai create instance 36440497 --image ...
Failed with error 400: Your account lacks credit; see the billing page.
```

**Available offers (for reference when funded):**
| Offer ID | GPU | VRAM | Price/hr | Reliability |
|----------|-----|------|----------|-------------|
| 36440497 | Titan Xp | 12GB | $0.048 | 1.00 |
| 37119888 | GTX 1080 | 8GB | $0.048 | 0.98 |
| 29017767 | RTX A2000 | 12GB | $0.052 | 1.00 |
| 34405894 | GTX 1080 Ti | 11GB | $0.059 | 0.99 |

**Impact:** The entire provisioner loop (`provisioner_loop.py`) is **non-functional** until Vast.ai account is funded. The pipeline will deadlock at the first render stage because jobs sit in `PENDING` forever.

### 3. B2 Credentials — FOUND

Located in `~/.env`:
```
B2_ACCOUNT_ID=039860cff55f
B2_APPLICATION_KEY=00462bc026af466457349e085feb5b8d2943c5dff3
B2_BUCKET_NAME=bearnaise-pipeline-artifacts
```

**Finding: Bucket name mismatch.**
- Code default: `cloudberry-documentary-v2`
- Actual credential: `bearnaise-pipeline-artifacts`

**Impact:** B2 uploads will fail unless `B2_BUCKET_NAME` is set correctly in the environment.

**Untested:** Could not run B2 upload/download test due to sandbox restrictions on the `server/` directory (see §5).

### 4. File Access Restrictions — SANDBOX BLOCKED

The shell environment cannot read files in `/Users/orpington/Documents/economy-documentary-work/server/`:

```
$ cat server/provisioner_loop.py
cat: server/provisioner_loop.py: Operation not permitted
```

Python imports also fail:
```
PermissionError: [Errno 1] Operation not permitted
```

**Impact:** Cannot run integration tests, cannot execute `provisioner_loop.py`, cannot test B2 upload/download from the codebase. The files ARE readable via the `ReadFile` tool (used successfully throughout this session), but the `Shell` tool is sandboxed from the Documents directory.

**Workaround:** Copy files to `/tmp` before execution, or run tests from a different directory.

### 5. Pre-existing Violations (Not Fixed)

These exist in files NOT modified in this session:

| Category | Count | Example Files |
|----------|-------|---------------|
| Timeouts | 28 | `worker_provisioner.py`, `infra_agent.py`, `agents/video_provisioner_agent.py` |
| Stubs (eval simulators) | 6 | `strands_agents/evals/simulators/` |
| Swallowed exceptions | 117 | `recovery.py`, `worker_provisioner.py`, `audio_stage.py`, `production_stage.py` |
| Fixed polling loops | 6 | `worker_provisioner.py`, `recovery.py` |
| Algorithmic retry | 18 | `recovery.py`, `worker_provisioner.py`, `fleet/scaler.py` |

These do not block the current architecture but may cause silent failures during pipeline execution.

## Blockers for Next Run

1. **Vast.ai credit = $0** — Must add funds before ANY VM provisioning works.
2. **B2 bucket name mismatch** — Set `B2_BUCKET_NAME=bearnaise-pipeline-artifacts` in environment.
3. **Sandbox file access** — Cannot run server code from shell. Use `PYTHONPATH=/tmp/...` or run from repo root with different permissions.

## Success Chance Assessment

**With Vast.ai funded and B2 bucket fixed: ~60%**

**Without Vast.ai funding: ~0%** (pipeline deadlocks at first render job)

### What will work (once funded):
1. Job queue submission by media agents
2. Provisioner claiming jobs and provisioning VMs
3. Worker boot + model load + generation
4. B2 upload of results
5. Agent polling, download, QA, OTIO assembly

### What might still break:
1. **VM boot time variance** — First boot can take 5-15 min. Provisioner has no timeout but graph `max_node_executions=50` limits total node visits.
2. **B2 SDK runtime** — `InMemoryAccountInfo` type error (pyright) may or may not manifest at runtime.
3. **Agent duplicate job submission** — `check_resume_status` returns `in_progress` but agent might misinterpret on graph re-invocation.
4. **Token cost** — Audio/video agents re-invoked multiple times via backward edges while waiting for renders.

## Recommendations

1. **Fund Vast.ai** — Add $10-20 credit. Test with one Titan Xp instance.
2. **Fix B2 bucket** — Export `B2_BUCKET_NAME=bearnaise-pipeline-artifacts` before pipeline start.
3. **Dry-run provisioner** — Start `python provisioner_loop.py` manually, submit one test job via queue, verify end-to-end flow.
4. **1-scene pipeline test** — Run full graph with 1 scene only. Cost: ~$0.10 VM + ~$0.50 tokens.
5. **Fix sandbox** — Either run from `/tmp` or adjust macOS sandbox permissions for the server directory.
