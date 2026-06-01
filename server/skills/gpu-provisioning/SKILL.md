---
name: gpu-provisioning
description: Provision and manage GPU VMs on Vast.ai for TTS and LTX-2.3 video generation workloads with parameter-aware resource matching
version: 1.1.0
tags:
  - provisioning
  - vastai
  - gpu
  - devops
  - ltx
author: pipeline
---

# GPU Provisioning Skill

You are an expert cloud GPU operator. This skill gives you deep knowledge of Vast.ai, GPU requirements for ML inference, and how to match VM specs to the actual parameters of the jobs being dispatched.

## Vast.ai Operations

**Search Syntax:**
```bash
vastai search offers "gpu_name in (RTX_4090, A100, H100) reliability > 0.95"
vastai search offers "rentable = true gpu_ram >= 48 num_gpus = 1"
```

## Parameter-Aware GPU Matching for LTX-2.3

The video generation workload is NOT one-size-fits-all. The GPU you provision MUST match the parameters the video agent plans to use. Ask yourself: what resolution, duration, and steps will the video agent request?

### The LTX-2.3 Resource Decision Tree

**Step 1: Determine the job profile**
- Resolution: 512×320 (standard) or 704×480 (high quality)?
- Duration: 4s (~97 frames) or 5s (~121 frames) or 8s (~185 frames)?
- Inference steps: 5 (fast), 20 (balanced), 30 (quality), 50 (polish)?
- Offload: none (fastest), cpu (slower, lower VRAM), disk (emergency)?
- Quantization: none (best quality), fp8-cast (slight quality loss, VRAM savings)?

**Step 2: Match GPU to profile**

| Profile | Required VRAM | Suitable GPUs | Cost/hr estimate |
|---------|--------------|---------------|------------------|
| 512×320, 4s, 20 steps, cpu+fp8 | ~18 GB | RTX 4090, A5000, A40 | $0.30-0.60 |
| 512×320, 5s, 30 steps, none | ~40 GB | A100 40GB, H100 | $0.80-1.50 |
| 512×320, 8s, 30 steps, none | ~48 GB | H100 80GB, H200 | $1.20-2.00 |
| 704×480, 5s, 20 steps, cpu+fp8 | ~32 GB | A100 40GB, H100 | $0.80-1.50 |
| 704×480, 5s, 30 steps, none | ~55 GB | H100 80GB | $1.20-2.00 |
| 704×480, 8s, 30 steps, none | ~72 GB | H200 only | $2.00-3.50 |
| 720p+, any duration | >80 GB | H200, multi-GPU | $3.00+ |

**Step 3: Fallback strategy if ideal GPU unavailable**
1. Reduce resolution: 704×480 → 512×320 (saves ~30% VRAM)
2. Reduce duration: 8s → 5s → 4s (saves ~15% per second reduction)
3. Enable cpu offload: saves ~40% VRAM, ~2× slower
4. Enable fp8-cast quantization: saves ~10-15% VRAM, minimal quality loss
5. Reduce steps: 30 → 20 → 10 (linear speedup, slight quality loss)
6. If all else fails: wait for better offers or provision multiple cheaper GPUs

### GPU Requirements by Workload

**Audio (Qwen3-TTS):**
| Workload | Minimum VRAM | Recommended GPUs | Notes |
|----------|-------------|------------------|-------|
| Qwen3-TTS | 24GB | RTX 4090, A5000, A40 | Fits comfortably |
| Qwen3-TTS batch | 40GB | A100 40GB | For parallel generation |

**Video (LTX-2.3):**
| Workload | Minimum VRAM | Recommended GPUs | Notes |
|----------|-------------|------------------|-------|
| 512×320 preview | 24GB | RTX 4090 + cpu offload + fp8 | Slow but works |
| 512×320 production | 40GB | A100 40GB, H100 | Good balance |
| 512×320 high-quality | 48GB | H100 80GB, H200 | Fast, no compromises |
| 704×480 production | 55GB | H100 80GB | Minimum for HD feel |
| 704×480 premium | 72GB | H200 | Best quality at HD |

**Instance Creation:**
```bash
vastai create instance <offer_id> \
  --image pytorch/pytorch:2.7.0-cuda12.6-cudnn9-runtime \
  --disk 150 --ssh --direct \
  --label documentary-<mode> \
  --onstart-cmd '<bootstrap>'
```

**Health Verification:**
- After provisioning, poll `GET /` on worker URL
- Expected response: `ok {gpu} tts={yes|no} ltx={yes|no} vram={used}/{total}GB mode={mode}`
- Avoid premature teardowns: TIME BASED TIMEOUTS ARE FORBIDDEN, TERMINATER PROCESS UPON OBJECTIVE CRITERIA. Do not destroy the VM if it is still loading or pulling the Docker image. Only terminate the process or VM if there is an objective, verified error (e.g. invalid Docker image tag or host hardware failure). If unreachable, check logs/diagnostics via SSH (like docker logs, nvidia-smi) to investigate the issue instead of destroying.

**Cost Optimization:**
- Never double-rent: Only one VM can be active at a time. If you need/want to provision a different VM, you must first destroy the existing active VM before renting the new one. Always use 'yes | vastai destroy instance <instance_id>' to avoid hanging on a confirmation prompt.
- Progressive Rollout (VM Scaling): If scaling up to multiple VMs in the future, you must ensure all currently active VMs are fully healthy and running without anomalies before provisioning any additional VMs.
- Keep VMs alive: Never destroy VMs based on idle time or runtime duration. Only release/destroy them when all queued jobs are complete and the coordinator signals teardown.
- Prefer spot/preemptible instances for non-urgent work
- Track per-job GPU cost; target <$0.50 per 30-second documentary
- If a job profile needs H200 but only H100 is available, adjust parameters instead of over-provisioning

**SSH Diagnostics:**
```bash
vastai ssh <instance_id> -- cat /workspace/agent.log
vastai ssh <instance_id> -- nvidia-smi
vastai ssh <instance_id> -- ps aux | grep python
```

## Negotiating with the Video Agent

When the video agent requests a render and no suitable GPU is available, you have two options:

1. **Provision a better GPU** — If budget allows and offers exist, provision up.
2. **Negotiate parameter reduction** — Tell the video agent to adjust:
   - "H200 unavailable. Can you work with H100 80GB? If so, limit to 512×320 @ 5s, 30 steps."
   - "A100 40GB only. Enable cpu offload and fp8-cast. Reduce to 512×320 @ 4s, 20 steps."

The video agent understands the parameter table. It can adjust prompts for shorter duration or simpler motion to compensate for lower steps.

## Self-Directed Research

If you encounter provisioning failures or need to optimize costs:
- Use `RESEARCH: Vast.ai <error>` for quick troubleshooting
- Use `RESEARCH_DEEP: Vast.ai pricing optimization GPU cloud 2025` for cost strategies
- Use `RESEARCH_NEWS: Vast.ai new features instances` for platform updates

Valuable research topics:
- New GPU models on Vast.ai (e.g., RTX 5090, B200)
- Alternative providers (RunPod, Lambda, FluidStack) for price comparison
- Docker image optimizations for faster boot times
- CUDA version compatibility matrices
