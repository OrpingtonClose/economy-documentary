---
name: pipeline-troubleshooting
description: Diagnose and resolve issues in the documentary pipeline — stuck jobs, failed VMs, quality problems, and cascading failures
version: 1.0.0
tags:
  - troubleshooting
  - debugging
  - pipeline
  - orchestration
author: pipeline
---

# Pipeline Troubleshooting Skill

You are an expert systems diagnostician. This skill gives you deep knowledge of debugging distributed pipelines, identifying root causes, and prescribing precise fixes.

## Diagnostic Framework: The 5 Whys

When something fails, ask "why" recursively until you reach a actionable root cause:

1. **What failed?** (symptom)
2. **Why did it fail?** (immediate cause)
3. **Why did that condition exist?** (contributing factor)
4. **Why wasn't it caught earlier?** (process gap)
5. **What single action prevents recurrence?** (fix)

## Common Failure Modes

**Audio Issues:**
- "Abrupt cut" → Text too long for TTS token budget → Split into shorter chunks
- "Trailing silence" → Generation truncated → Reduce text length or retry
- "Wrong voice mood" → Voice-scene mismatch → Remap voice selection
- "No audio output" → VM crash or OOM → Check worker health, re-provision

**Video Issues:**
- "Frozen frames" → Prompt lacks motion keywords → Add camera/natural motion
- "Too short" → GPU OOM during generation → Reduce duration or simplify prompt
- "Corrupted file" → Worker crash mid-write → Retry on fresh VM
- "Color banding" → Insufficient bit depth or poor lighting spec → Add lighting detail

**VM/Provisioning Issues:**
- "No offers available" → GPU supply low → Wait and retry, or lower requirements
- "VM unreachable" → Network or worker crash → SSH check logs, or destroy/re-provision
- "VM idle but burning credits" → Job dispatch failure → Check worker URL registration
- "Provisioning error" → Incompatible image or disk full → Increase disk, use verified image

**Assembly Issues:**
- "ffmpeg invalid data" → Corrupted input file → Re-generate the corrupt media
- "Silent output" → ffmpeg failed silently → Check output size, verify codecs
- "Duration mismatch" → Audio and video lengths differ → Loop or trim as appropriate

## Escalation Rules

- **1 retry:** Same parameters, transient failure
- **2 retries:** Adjusted parameters (shorter text, simpler prompt, different voice)
- **3+ retries:** Escalate — change approach entirely (different model, different scene, skip)
- **VM fails 2×:** Destroy and provision fresh VM
- **All VMs failing:** Check network, API keys, or Vast.ai status

## Self-Directed Research

If you encounter an unfamiliar error or need deeper context:
- Use `RESEARCH: <error message>` for quick answers
- Use `RESEARCH_DEEP: <technical topic>` for comprehensive understanding
- Use `RESEARCH_NEWS: <topic>` for recent developments or known issues

Research is most valuable when:
- A new error code appears from Vast.ai or a model
- You suspect a bug in a dependency (ffmpeg, CUDA, etc.)
- You need to compare alternative approaches or tools
- A model's behavior has changed (new version, different outputs)
