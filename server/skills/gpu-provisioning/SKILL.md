---
name: gpu-provisioning
description: Provision and manage GPU VMs on Vast.ai for TTS and video generation workloads
version: 1.0.0
tags:
  - provisioning
  - vastai
  - gpu
  - devops
author: pipeline
---

# GPU Provisioning Skill

You are an expert cloud GPU operator. This skill gives you deep knowledge of Vast.ai, GPU requirements for ML inference, and VM lifecycle management.

## Vast.ai Operations

**Search Syntax:**
```bash
vastai search offers "gpu_name in (RTX_4090, A100, H100) reliability > 0.95"
vastai search offers "rentable = true gpu_ram >= 48 num_gpus = 1"
```

**GPU Requirements by Workload:**

| Workload | Minimum VRAM | Recommended GPUs | Notes |
|----------|-------------|------------------|-------|
| Qwen3-TTS (audio) | 24GB | RTX 4090, A5000, A40 | Fits in 24GB comfortably |
| LTX-2.3 512×320 | 40GB | A100 40GB, RTX A6000 | Minimum for basic video |
| LTX-2.3 720×480 | 48GB | H100, H200, A100 80GB | Higher quality, slower |
| LTX-2.3 720p+ | 80GB | H100, H200 | Future-proofing |

**Instance Creation:**
```bash
vastai create instance <offer_id> \
  --image nvidia/cuda:12.6.0-cudnn9-runtime-ubuntu22.04 \
  --disk 150 --ssh --direct \
  --label documentary-<mode> \
  --onstart-cmd '<bootstrap>'
```

**Health Verification:**
- After provisioning, poll `GET /` on worker URL
- Expected response: `ok {gpu} tts={yes|no} ltx={yes|no} vram={used}/{total}GB mode={mode}`
- If unreachable after 5 minutes: SSH to `/workspace/agent.log` or destroy and retry

**Cost Optimization:**
- Destroy idle VMs after 30 minutes of no jobs
- Destroy VMs running > 4 hours unless actively processing
- Prefer spot/preemptible instances for non-urgent work
- Track per-job GPU cost; target <$0.50 per 30-second documentary

**SSH Diagnostics:**
```bash
vastai ssh <instance_id> -- cat /workspace/agent.log
vastai ssh <instance_id> -- nvidia-smi
vastai ssh <instance_id> -- ps aux | grep python
```

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
