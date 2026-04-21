# Judge Fleet Provisioning Playbook

This document is the runbook for standing up the self-hosted judge
fleet that the Tier-2 evals (PR-E) grade atomic components against.
It covers: catalog, B2 layout, Vast.ai provisioning, health checks,
and teardown.

The judge stack is **local-first** — Gemma 4 abliterated, Qwen3.5-Omni,
and video-SALMONN 2 72B run on our Vast.ai fleet under `judge_worker.py`.
Proprietary APIs (Claude, GPT, Gemini) are only reachable through the
`FallbackJudgeClient` and must be explicitly opted-in per evaluator;
they never drive the primary adjudication path.

## Catalog

| Key                     | Role             | VRAM  | Disk  | Source                      |
|-------------------------|------------------|-------|-------|-----------------------------|
| `gemma4_abliterated`    | safety           | 60 GB | 180 GB | B2 only (`models/judges/gemma4-abliterated/`) |
| `qwen35_omni`           | av_primary       | 72 GB | 220 GB | HF hub + B2 mirror          |
| `video_salmonn_2_72b`   | av_tiebreaker    | 160 GB | 400 GB | HF hub + B2 mirror          |

All three are defined in
`server/strands_agents/judges/models.py`; the registry there is the
authoritative source and any change to hardware requirements MUST be
made by editing a :class:`JudgeModelSpec` (not by tweaking Vast.ai CLI
arguments).

## B2 layout

The pipeline's default bucket (`cloudberry-documentary-v2` by default,
overridable via `B2_BUCKET_NAME`) hosts the judge weights at:

```
models/
  judges/
    gemma4-abliterated/
      config.json
      tokenizer.json
      tokenizer_config.json
      model.safetensors.index.json
      model-0000{1..6}-of-00006.safetensors
    qwen3.5-omni/
      config.json
      tokenizer.json
      preprocessor_config.json
      model.safetensors.index.json
    video-salmonn-2-72b/
      config.json
      tokenizer.json
      model.safetensors.index.json
```

Uploading new judge weights is a one-time administrator operation and
is intentionally out-of-scope for `judges/fetcher.py`. Use the `b2`
CLI or a one-off script when a new abliterated build drops.

## Provisioning one judge VM

```python
from strands_agents.judges import (
    GEMMA4_ABLITERATED,
    build_judge_worker_spec,
)

# Translate the catalog entry into a worker spec the existing
# provisioner understands.
spec = build_judge_worker_spec(
    GEMMA4_ABLITERATED,
    local_port=8881,
    max_price=6.0,
)

# Hand the spec to the standard Vast.ai provisioner (TTS/LTX already
# use this path — see server/worker_provisioner.py).
from worker_provisioner import provision_worker
provision_worker(spec)
```

The provisioner:

1. Searches Vast.ai for an offer matching `spec.gpu_type`,
   `spec.min_vram_gb`, `spec.min_disk_gb`, and `spec.max_price`.
2. Creates the instance with a PyTorch Docker image compatible with
   `spec.min_torch` / `spec.min_cuda` (picked via
   `resolve_docker_image("judge_<key>")`).
3. Bootstraps the worker with `scripts/judge_worker.py --mode
   judge_<key> --port 8880`.
4. Polls `/health` until the model finishes loading (judge workers
   can take 5–15 minutes to hydrate the first time because they pull
   ~50–150 GB of weights from B2).

## Staggered model loading

The three judges together exceed any single H100 SXM5's VRAM budget.
Three approaches:

1. **Three separate VMs** (recommended, simplest). Each judge gets its
   own VM; the ensemble routes by URL. Cost: ~$15–25/hr wall-clock.
2. **One VM with sequential loading via StateDictRegistry** (mirrors
   LTX's approach in `scripts/gpu_worker.py:800`). Use only when the
   eval job is known to exercise one judge at a time, e.g. a content-
   rejection gauntlet that only needs Gemma 4. Cost: ~$6–8/hr.
3. **One VM per role with quantisation** (experimental). 4-bit
   quantised SALMONN fits on an A100 80 GB. Lowers fidelity; use only
   if evals have shown the quantised verdict tracks the full-precision
   verdict closely enough for the rubric.

The judge worker script will gate its loader behind
`JUDGE_LOAD_MODE=sequential` vs `JUDGE_LOAD_MODE=parallel` to make the
choice explicit rather than implicit.

## Health check

Every judge exposes:

- `GET /health` — returns `{"status": "ok", "model": "<id>",
  "ready": true}` once loaded.
- `GET /ready` — liveness probe (returns `{"status": "ok"}` as soon as
  the FastAPI process is up, even before weights finish loading).
- `POST /v1/chat/completions` — OpenAI-compatible endpoint; the shape
  is defined by `JudgeRequest.to_payload()`.

Warmup after provisioning: issue one `chat.completions` call per judge
with a known prompt (e.g. the safety judge gets a known-unsafe snippet;
the AV judge gets a tiny 1-second stub video) and confirm the score
lands in the expected band. This is what the integration test suite
(shipped in PR-C / PR-E) does.

## Teardown

```
# Preferred: let the provisioner destroy the VMs it registered.
from tools.vastai_tools import destroy_all_owned_vms
destroy_all_owned_vms()
```

Judges left running on Vast.ai over a weekend will eat the credit
budget; the owner-VM registry tracks everything the provisioner
created in the current session so teardown is a one-call operation.

## Wire contract

Every judge responds to the same request shape:

```json
{
  "model": "<id>",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": [
      {"type": "text", "text": "..."},
      {"type": "image_url", "image_url": {"url": "..."}},
      {"type": "audio_url", "audio_url": {"url": "..."}},
      {"type": "video_url", "video_url": {"url": "..."}}
    ]}
  ],
  "temperature": 0.0,
  "max_tokens": 1024
}
```

…and returns an OpenAI chat.completions response. This lets
evaluators dispatch the same `JudgeRequest` to multiple judges in
parallel without per-backend branching — the behaviour
`JudgeEnsemble` (shipped in PR-C) depends on.
