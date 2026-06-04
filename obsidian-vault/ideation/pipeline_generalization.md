# Architectural Ideation: Transforming the Pipeline through Generic Capabilities and Hooks

This document explores how a transition to a fully generic, declarative `AbstractCapability` and event-hook architecture can generalize and optimize the media pipeline.

---

## 1. Dynamic Agent Personalization & Swarm Topology
* **The Present**: We have 5 hardcoded HTTP servers running fixed agent personas (`scenario`, `audio`, etc.) with hardcoded instruction sets.
* **The General Future**: We instantiate a pool of **Generic Agent Runners**. An agent is a blank slate. When the coordinator assigns a task, the agent dynamically mounts the required `SkillCapability` instances (e.g., `skill_audio_production` + `skill_pipeline_troubleshooting`).
* **Why it's better**: The pipeline becomes a fluid, self-organizing swarm. If there is a bottleneck in video rendering, the coordinator can spin up three more generic runners and dynamically mount the `video_generation` skill to them. The topology scales and shrinks dynamically.

---

## 2. Skill-Encapsulated Context Compaction
* **The Present**: Context compaction (`otio_aware_compress`) is hardcoded in the agent base. It has to know about OTIO slots and track types, making it tightly coupled to the documentary domain.
* **The General Future**: The compaction logic (the `on_before_compress` callback) is packaged **inside the skill capability itself**. 
  * `AudioProductionSkill` runs a compaction hook that condenses completed dialogues while retaining dirty ones.
  * `GPUProvisioningSkill` runs a compaction hook that collapses historical VM boots and IP addresses into a clean list of active instances.
* **Why it's better**: Context windows are kept incredibly small, clean, and highly domain-optimized without leaking domain-specific knowledge into the core agent framework.

---

## 3. "Shadow Mode" & Verification (Simulation)
* **The Present**: Testing the pipeline end-to-end requires running real VMs or building complex mock scripts.
* **The General Future**: We load a generic `SimulationCapability` hook. 
  * When a `QueueJob` event is parsed, the simulation hook intercepts it, bypasses the actual VM call, and writes a mock `JobCompleted` event to the store with simulated durations and measurements.
  * We can run the entire pipeline in "Shadow Mode" to test script-writing flow, scheduling, and error-handling paths in under 10 seconds without executing a single cloud GPU command or rendering a single wave file.
* **Why it's better**: Continuous Integration (CI) and pipeline dry-runs become trivial. We can verify that agent prompts, event ordering, and coordination logic are structurally sound before deploying.

---

## 4. Invisible Boundary Enforcement (Security & Budgets)
* **The Present**: Agents must be repeatedly instructed in their system prompts to ignore video tracks, check cost limits, or only run allowlisted bash commands. This consumes system prompt tokens and is subject to LLM compliance drift (jailbreaking/hallucinations).
* **The General Future**: Guardrails are enforced at the capability level using `on_tool_call` and `pre_run` hooks.
  * If the agent attempts to run a bash command not in the allowlist, the `SecurityCapability` hook catches it and returns a standard error message directly to the agent's loop before execution.
  * If the cumulative spend is approaching the limit, the `CostTrackingCapability` intercepts the prompt and appends a warning banner before the LLM even sees the turn.
* **Why it's better**: Prompt sizes decrease, and safety/security boundaries become hard invariants enforced by runtime code, rather than suggestions in prose that the agent might ignore.

---

## 5. Self-Healing Tool Lifecycle Hooks
* **The Present**: If a tool fails (e.g. Vast.ai API rate limits or network timeout), the agent either loops indefinitely or crashes.
* **The General Future**: The capability defines an `on_tool_error` hook.
  * If a Vast.ai call throws a `429 Too Many Requests`, the hook can implement an exponential backoff or automatically switch to a fallback cloud provider.
  * If a WhisperX run fails due to corrupted audio formats, the hook can automatically trigger a transcoding job to convert the audio, presenting the agent with a clean, corrected result transparently.
* **Why it's better**: The agent's decision-making loop is insulated from low-level operational failures, dramatically increasing runtime reliability.

---

## 6. No-Code Extensibility for Domain Experts
* **The Present**: Adding a new step to the media assembly (like adding subtitles, color grading, or watermarking) requires a software engineer to write Pydantic schemas, python endpoint code, and update the event store.
* **The General Future**: A creative director or video editor can write a new skill entirely in the Obsidian vault using Markdown and YAML. They define the event schema, the narrative prompt, and the validation rule (e.g., checking if the subtitle file exists in GSA). The next time the pipeline starts, the system compiles and runs it automatically.
* **Why it's better**: The software development cycle is completely decoupled from the creative/editorial workflow. The pipeline runs as a generic operating system, and skills are simply apps loaded at runtime.
