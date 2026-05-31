> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Agentic Context Management: Research Brief
## From Passive Truncation to Self-Regulating Agent Memory

**Date:** 2026-05-27
**Sources:** Exa AI Search, arXiv, OpenReview, Anthropic Engineering Blog, OpenAI Cookbook, Vstorm OSS
**Query Focus:** Agent-driven context management — agents that decide when, what, and how to compress/summarize/curate their own memory

---

## Executive Summary

The field is rapidly shifting from **passive** context management (sliding window, fixed-interval summarization, external heuristics) to **agentic** context management, where the agent itself is empowered to:

1. **Decide when to compress** — not at fixed intervals, but at semantically meaningful moments
2. **Decide what to preserve** — selectively retaining reasoning anchors vs. pruning noise
3. **Execute memory operations as actions** — compression, retrieval, forgetting are tool calls like any other
4. **Learn optimal strategies via RL** — end-to-end training of summarization timing and content

For ARCHITECTURE_V7.1, this suggests a third paradigm beyond "orchestration-layer rules" (Part I) and "neuro-symbolic memory" (Part II): **agent-driven self-regulation**. The `compact_conversation` tool already exists in `summarization-pydantic-ai`. The architecture should explicitly design for agentic memory operations rather than treating context as something that merely happens *to* the agent.

---

## Part I: The Agentic Paradigm — Core Concepts

### 1.1 Memory as Action (AgeMem, Memory-as-Action)

**Papers:**
- *Agentic Memory: Learning Unified Long-Term and Short-Term Memory Management for Large Language Model Agents* (Yu et al., arXiv:2601.01885, Alibaba/Wuhan University)
- *Memory as Action: Autonomous Context Curation for Long-Horizon Agentic Tasks* (Zhang et al., arXiv:2510.12635, Beijing Jiaotong/Huawei)

#### Core Thesis

Traditional frameworks treat memory management as an external controller:
```
[Agent] → [External Memory Manager] → [Truncated Context] → [Agent]
```

Agentic frameworks unify memory operations into the agent's policy:
```
[Agent] → decides: store? retrieve? summarize? forget? → executes as tool call → [Agent]
```

**AgeMem** exposes five tool-based actions:
| Action | Description |
|--------|-------------|
| `STORE` | Persist task-relevant information to long-term memory |
| `RETRIEVE` | Recall relevant past information |
| `UPDATE` | Modify existing memory entries |
| `SUMMARIZE` | Compress working memory into compact form |
| `DISCARD` | Remove irrelevant information |

The agent autonomously decides what and when via these actions, trained via a three-stage progressive RL strategy with step-wise GRPO to handle sparse/discontinuous rewards from memory operations.

**Memory-as-Action** introduces a critical challenge: *trajectory fractures*. When the agent edits its working memory (deleting or replacing messages), the standard assumption of a continuously growing prefix breaks. Standard policy gradient methods fail because the causal continuity of the trajectory is disrupted.

Solution: **Dynamic Context Policy Optimization (DCPO)** — segments trajectories at memory action points and applies trajectory-level advantages to resulting action segments.

#### Result Highlights

- AgeMem outperforms strong memory-augmented baselines across 5 long-horizon benchmarks
- Achieves improved task performance, higher-quality LTM, and more efficient context usage
- Memory-as-Action reduces computational consumption while improving task performance through adaptive curation

---

### 1.2 Self-Sum — Teaching Agents When and What to Summarize

**Paper:** *Self-Sum: Teaching Agent Itself to Decide When and What to Summarize* (OpenReview ACL ARR 2026, Submission 4874)

#### Core Thesis

Rule-based summarization ("every N steps") is inflexible and introduces irreversible information loss. Self-Sum models summarization as a **first-class internal cognitive action**, unified with external environmental actions.

#### Two-Stage Training Recipe

1. **Cold-start SFT:** Bootstraps summarization behavior from demonstration data
2. **Lightweight summarization-aware RL:** Refines timing and content while discouraging unnecessary summaries

#### Key Finding

Self-Sum learns to summarize **sparsely at meaningful moments** — not at fixed intervals, but when the agent detects context saturation or task-phase transitions. It preserves task-relevant information and generalizes better than rule-based baselines.

#### Implication for V7.1

The `compact_conversation` tool in `summarization-pydantic-ai` is the infrastructure equivalent. But rather than merely "making it available," the agent should be **prompted and evaluated** on its ability to invoke compaction strategically. The prompt engineering in §4 of V7.1 should include explicit guidance on when to compact.

---

### 1.3 Focus — Active Context Compression

**Paper:** *Active Context Compression: Autonomous Memory Management in LLM Agents* (Verma, arXiv:2601.07190)

#### Core Thesis

Inspired by *Physarum polycephalum* (slime mold) exploration strategies, the Focus Agent:
1. **Autonomously decides** when to consolidate key learnings into a persistent "Knowledge" block
2. **Actively withdraws** (prunes) raw interaction history after consolidation

#### Architecture

```
[Knowledge Block] — persistent, agent-managed summary of key learnings
[Raw History] — pruned after consolidation
[Current Turn] — always preserved
```

#### Results (SWE-bench Lite, evaluated with a lightweight frontier model)

| Metric | Baseline | Focus |
|--------|----------|-------|
| Accuracy | 60% (3/5) | 60% (3/5) |
| Total Tokens | 14.9M | 11.5M |
| Token Reduction | — | **22.7%** |
| Avg Compressions/Task | — | 6.0 |
| Max Single-Instance Savings | — | **57%** |

**Critical insight:** The agent achieved identical accuracy with 22.7% fewer tokens. Capable models can autonomously self-regulate when given appropriate tools and prompting.

---

### 1.4 MemPO — Self-Memory Policy Optimization

**Paper:** *MemPO: Self-Memory Policy Optimization for Long-Horizon Agents* (Li et al., arXiv:2603.00680, Tsinghua/Alibaba)

#### Core Thesis

Existing external memory modules prevent the model from **proactively managing** its own memory. MemPO enables the policy model to autonomously summarize and manage memory during environment interaction.

#### Key Innovation: Credit Assignment via Memory Effectiveness

Standard RL credit assignment struggles with memory operations because their benefit is delayed and diffuse. MemPO improves credit assignment by explicitly measuring memory effectiveness — how much a saved/removed piece of information contributed to later success.

#### Results

| Metric | Base Model | MemPO | Gain |
|--------|------------|-------|------|
| F1 Score | baseline | +25.98 absolute | — |
| vs. Previous SOTA | — | +7.1 | — |
| Token Usage Reduction | — | **67.58%** | — |

---

### 1.5 ContextBudget — Budget-Aware Context Management

**Paper:** *ContextBudget: Budget-Aware Context Management for Long-Horizon Search Agents* (Wu et al., arXiv:2604.01664, Zhejiang/Alibaba)

#### Core Thesis

Context management should be formulated as a **sequential decision problem with a context budget constraint**. The agent assesses available budget before incorporating new observations and decides when/how much to compress.

#### Two Failure Modes of Budget-Free Compression

1. **Under relaxed budgets:** Agents over-compress and erase task-critical evidence
2. **Under tight budgets:** Agents under-compress and hit hard limits, truncating arbitrarily

#### BACM-RL: Curriculum-Based RL for Compression Strategies

Trains compression strategies under varying context budgets. On compositional multi-objective QA and web browsing:

- **>1.6x gains** over strong baselines in high-complexity settings
- Maintains advantage as budgets shrink (where baselines trend downward)
- Outperforms across model scales and task complexities

#### Implication for V7.1

The `EventStore` JSONL architecture (§5) naturally supports budget tracking — `seq` is monotonic, and token estimates can be accumulated per run. A simple budget-aware wrapper could expose "context budget remaining" to agents as part of their observation space.

---

### 1.6 ActiveContext / CORAL — Specialized Curator Agents

**Papers:**
- *Escaping the Context Bottleneck: Active Context Curation for LLM Agents via RL* (Li et al., arXiv:2604.11462, Tongji/Stanford)
- *Don't Lose the Thread: Cognitive Resource Self-Allocation* (Zhu et al., OpenReview ICLR 2026, CORAL)

#### Core Thesis

**Decouple context management from task execution.** Use a lightweight, specialized policy model to curate working memory for a frozen foundation model.

#### ActiveContext Architecture

```
[TaskExecutor] — powerful frozen foundation model (handles reasoning + execution)
      ↑↓
[ContextCurator] — lightweight RL-trained policy (handles memory curation)
```

The ContextCurator performs **active information entropy reduction**:
- Aggressively prunes environmental noise (e.g., 90% of DOM tree is structural noise)
- Meticulously preserves **reasoning anchors** — sparse data points critical for future deductions

#### Results

| Benchmark | Baseline | ActiveContext | Tokens |
|-----------|----------|---------------|--------|
| WebArena | 36.4% | **41.2%** | 47.4K → 43.3K (−8.8%) |
| DeepSearch | 53.9% | **57.1%** | **8× reduction** |

A **7B ContextCurator matches GPT-4o's** context management performance.

#### CORAL Architecture

COgnitive Resource Self-ALlocation (CORAL) implements a **checkpoint-based** working memory:
1. Agent maintains crucial checkpoints of progress
2. When memory becomes cluttered, initiates a new problem-solving episode
3. Purges cluttered working memory
4. Resumes reasoning from most recent checkpoint

Trained via **Multi-episode Agentic Reinforced Policy Optimization.**

---

### 1.7 Acon — Guideline-Optimized Compression

**Paper:** *Acon: Optimizing Context Compression for Long-horizon LLM Agents* (Kang et al., arXiv:2510.00615, KAIST/Microsoft)

#### Core Thesis

Context compression can be optimized via **natural-language guideline optimization**. Given paired trajectories where full context succeeds but compressed context fails, capable LLMs analyze failure causes and update compression guidelines accordingly.

#### Pipeline

1. Collect trajectory pairs: (full context → success, compressed → failure)
2. LLM analyzes why compression caused failure
3. Update natural-language compression guidelines
4. Distill optimized compressor into smaller models

#### Results

- **26–54% peak token reduction** on AppWorld, OfficeBench, Multi-objective QA
- **>95% accuracy preserved** when distilled into smaller compressors
- Enhances smaller LMs as agents by up to **46% performance improvement**

---

### 1.8 SUPO — Summarization for RL Training

**Paper:** *Scaling LLM Multi-turn RL with End-to-end Summarization-based Context Management* (Lu et al., arXiv:2510.06727, ByteDance/Stanford/CMU)

#### Core Thesis

Context length is a **fundamental bottleneck for RL training** of long-horizon agents, not just inference. Existing RL pipelines suffer from:
1. Degenerated instruction following on very long contexts
2. Excessive rollout costs (longer contexts → longer rollouts)
3. Hard context limits preventing sufficiently long-horizon tasks

#### Solution: Summarization-Augmented Policy Optimization (SUPO)

Periodically compress tool-use history via LLM-generated summaries during RL training. The policy gradient is derived to optimize **both** tool-use behaviors and summarization strategies end-to-end.

#### Key Result

SUPO improves success rate while maintaining the same or **lower** working context length vs. baselines. For complex search tasks, performance improves when scaling test-time summarization rounds beyond training-time levels.

---

## Part II: Industry & Ecosystem Implementations

### 2.1 Anthropic: Context Engineering as a Discipline

**Source:** *Effective context engineering for AI agents* (Anthropic Engineering Blog, Sep 2025)

Anthropic frames context engineering as the **natural progression** of prompt engineering:
- Prompt engineering = writing optimal instructions
- Context engineering = curating and maintaining the optimal token set during inference

Key strategies:
1. **Progressive disclosure** — don't show everything upfront; reveal context as needed
2. **Structured context** — use clear delimiters, headers, and schemas
3. **Active maintenance** — regularly audit what's in context and why

#### Implication for V7.1

The effect-based architecture (§3) already uses structured context (typed effects with `kind` discriminant). The gap is **active maintenance** — agents should be empowered to audit and manage their own effect history.

---

### 2.2 OpenAI: Session Memory Management

**Source:** OpenAI Agents SDK Cookbook — *Short-Term Memory Management with Sessions*

OpenAI's Agents SDK provides session-level memory management where:
- Tools can be dynamically added/removed per session
- Context is scoped to session boundaries
- Handoffs between agents can carry selective context

#### Implication for V7.1

The `run_id` field in the `Effect` base class (§3) already scopes effects to sessions. A natural extension is **context pruning per run** — when a run completes, the agent decides which effects from that run to persist vs. archive.

---

### 2.3 Pydantic-Deep: Agent-Triggered Compression

**Source:** `summarization-pydantic-ai` v0.1.3+, `pydantic-deep` v0.3.3+

The pydantic-deep ecosystem already supports agentic context management:

```python
from pydantic_ai_summarization import ContextManagerCapability

agent = Agent(
    "deepseek-v4-flash",
    capabilities=[ContextManagerCapability(
        max_tokens=100_000,
        include_compact_tool=True,  # Agent gets compact_conversation(focus?) tool
    )],
)
```

The agent can call `compact_conversation(focus="preserve API design decisions")` to trigger compression with a focus topic. Compression is deferred to the next model request.

**pydantic-deep v0.3.3+** also introduced:
- **ACP (Agent Control Protocol)** — structured agent-agent communication
- **Lifecycle hooks** — `on_before_compress`, `on_after_tool_execute`
- **Thinking** — explicit reasoning steps before action

#### Implication for V7.1

The architecture already uses `create_deep_agent()` with `on_before_compress`. The missing pieces are:
1. **Explicit `include_compact_tool=True`** in capability configuration
2. **Prompt guidance** on when/why to compact (§4)
3. **Effect logging** for compaction events (which effects were summarized?)

---

## Part III: Comparative Taxonomy of Agentic Context Management

| Approach | Decision Maker | Trigger | What Gets Preserved | Training | Key Metric |
|----------|---------------|---------|-------------------|----------|------------|
| **Self-Sum** | Agent itself | Learned via RL | Task-relevant info | SFT + RL | Sparse, meaningful summarization |
| **Focus** | Agent itself | Agent-detected saturation | "Knowledge" block | Prompting + scaffold | 22.7% token reduction, same accuracy |
| **AgeMem** | Agent itself | Tool-based actions (store/retrieve/summarize/discard) | LTM + curated STM | 3-stage progressive RL | Unified LTM/STM management |
| **Memory-as-Action** | Agent itself | Learned via DCPO | Reasoning anchors | RL (DCPO) | Reduced compute + improved performance |
| **MemPO** | Agent itself | Memory effectiveness credit | Crucial information | RL with improved credit | 67.58% token reduction |
| **ContextBudget** | Agent itself | Budget assessment before new obs | Budget-dependent evidence | Curriculum RL | >1.6x gains under tight budgets |
| **ActiveContext** | Separate curator agent | Entropy reduction policy | Reasoning anchors | RL (lightweight curator) | 7B curator ≈ GPT-4o curation |
| **CORAL** | Agent itself | Checkpoint-based episode reset | Checkpoints of progress | Multi-episode RL | Sharpened attention on checkpoints |
| **Acon** | External optimizer (distilled to agent) | Failure analysis | Guidelines-defined critical info | Guideline optimization + distillation | 26–54% peak reduction |
| **SUPO** | Agent itself | Periodic during RL training | Task-relevant summary | End-to-end RL | Scales beyond fixed context limit |

---

## Part IV: Recommendations for ARCHITECTURE_V7.1 → V7.2

### Tier 1: Immediate — No Code Changes Required (Prompt + Config)

1. **Add agentic compaction guidance to §4 (Prompt-Based Rules)**
   - Include explicit instructions on *when* to call `compact_conversation()`
   - Guidance: "When you notice repeated patterns, completed sub-tasks, or context exceeding 70%, compact with a focus topic"
   - The `focus` parameter is critical — it tells the summarizer what to preserve

2. **Enable `include_compact_tool=True` in all pipeline agents**
   - Current V7.1 does not explicitly enable this
   - Add to `ContextManagerCapability` in §9 per-agent configurations

3. **Add compaction effects to §3 (Effect schemas)**
   ```python
   class ContextCompacted(Effect):
       kind: Literal["context_compacted"] = "context_compacted"
       focus: str | None = None
       messages_before: int
       messages_after: int
       summary: str
   ```
   - This makes compaction auditable and replayable

### Tier 2: Short-Term — Minor Architecture Extensions

4. **Implement `ContextBudget` wrapper around EventStore**
   - Track cumulative token estimate per `run_id`
   - Expose "budget remaining" to agents via tool or system message
   - Budget = `max_context_tokens - current_estimate`
   - When budget < 30%, inject URGENT; < 15%, inject CRITICAL

5. **Add reasoning anchor detection**
   - Before compaction, tag messages as "anchor" or "ephemeral"
   - Anchors: decisions, commitments, key findings
   - Ephemeral: tool output, intermediate calculations, failed attempts
   - The agent (or a lightweight curator) tags messages; compaction preserves anchors

6. **Per-agent memory specialization**
   - `Researcher`: Preserve citations, source credibility assessments
   - `Scriptwriter`: Preserve narrative arc decisions, character beats
   - `Editor`: Preserve cut decisions, timing markers
   - Each agent gets a `memory_guidelines` field in Config (§14)

### Tier 3: Medium-Term — New Components

7. **Lightweight ContextCurator sub-agent**
   - A separate, smaller agent (or rule-based curator) that manages context for the main task agent
   - Inspired by ActiveContext's 7B curator matching a powerful model's curation
   - Could be a deterministic module initially (regex-based anchor detection), graduating to learned

8. **Checkpoint-based episode management (CORAL-style)**
   - At task-phase boundaries, agent saves a checkpoint
   - If context becomes cluttered, agent can "rewind" to checkpoint + compacted summary
   - Checkpoints are `Effect` instances: `CheckpointSaved(phase, key_decisions)`

9. **Summarization quality feedback loop**
   - After compaction, if the agent later requests information that was in the pruned context, flag a "compaction miss"
   - Use misses to refine compaction guidelines (Acon-style)
   - Could be automated: if agent re-executes a tool call whose output was previously in context, the compaction was too aggressive

### Tier 4: Long-Term — Research Integration

10. **Evaluate end-to-end RL training for compaction**
    - SUPO-style: if agents are fine-tuned via RL, include summarization in the policy
    - Only justified if pipeline runs at scale (1000s of episodes)

11. **Multi-agent memory coordination**
    - When agents hand off to each other, what context transfers?
    - Inspired by AgeMem's unified LTM: a shared memory pool with agent-specific retrieval

---

## Part V: Architecture Document Amendments

### New §4.x — Agentic Context Management Rules

Add a subsection to §4 (Prompt-Based Rules) covering:
- When to compact (thresholds, task-phase boundaries)
- How to set `focus` parameter for meaningful summarization
- What to treat as reasoning anchors (decisions, not observations)
- Budget awareness (how to read remaining context budget)

### New §3.x — Compaction Effect Schema

```python
class ContextCompacted(Effect):
    kind: Literal["context_compacted"] = "context_compacted"
    run_id: str
    agent: str
    focus: str | None
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    summary: str
    preserved_anchors: list[str]  # IDs of messages tagged as anchors
    timestamp: datetime
```

### Updated §9 — Per-Agent Capability Configuration

```python
create_deep_agent(
    model="deepseek-v4-flash",
    capabilities=[
        LimitWarnerCapability(max_context_tokens=100_000),
        ContextManagerCapability(
            max_tokens=100_000,
            compress_threshold=0.85,  # Lower from 0.9 — let agent decide at 85%
            max_tool_output_tokens=5000,
            include_compact_tool=True,  # NEW: enable agent-triggered compaction
        ),
    ],
    memory_guidelines="Preserve: source credibility, key facts, rejected hypotheses. "
                     "Discard: raw search results, intermediate calculations.",
)
```

### Updated §14 — Config Model Additions

```python
class Config(BaseModel):
    # ... existing fields ...
    context_window_tokens: int = 100_000
    compress_threshold: float = 0.85
    max_tool_output_tokens: int = 5000
    enable_agentic_compact: bool = True
    memory_guidelines: dict[str, str] = {}  # agent_id → guidelines
    context_budget_warning_threshold: float = 0.30  # 30% remaining = URGENT
    context_budget_critical_threshold: float = 0.15  # 15% remaining = CRITICAL
```

---

## Part VI: Synthesis — The Three Paradigms

| Paradigm | Mechanism | Maturity | V7.1 Applicability |
|----------|-----------|----------|-------------------|
| **Passive** (§I of prior research) | Sliding window, fixed-interval summarization, external heuristics | Production | Already configured |
| **Agentic** (this document) | Agent decides when/what to compress; memory as action; budget awareness | Emerging (2025–2026) | Immediately applicable via `compact_conversation` tool + prompt engineering |
| **Neuro-Symbolic** (§II of prior research) | Structured episodic memory, knowledge graphs, spatial indexing | Research | Future path for 10K+ event scale |

**The strategic recommendation:** Implement Tier 1 and Tier 2 immediately. The infrastructure already exists in `summarization-pydantic-ai`. The missing piece is **architectural intent** — designing the pipeline as a system where agents actively manage their own memory rather than passively accepting whatever the orchestration layer provides.

---

## References

1. Verma, N. (2026). "Active Context Compression: Autonomous Memory Management in LLM Agents." arXiv:2601.07190.
2. Yu, Y., Yao, L., et al. (2026). "Agentic Memory: Learning Unified Long-Term and Short-Term Memory Management for Large Language Model Agents." arXiv:2601.01885.
3. Zhang, Y., Shu, J., et al. (2025). "Memory as Action: Autonomous Context Curation for Long-Horizon Agentic Tasks." arXiv:2510.12635.
4. Anonymous. (2026). "Self-Sum: Teaching Agent Itself to Decide When and What to Summarize." OpenReview ACL ARR 2026, Submission 4874.
5. Li, R., Zhang, X., et al. (2026). "MemPO: Self-Memory Policy Optimization for Long-Horizon Agents." arXiv:2603.00680.
6. Wu, Y., Zheng, Y., et al. (2026). "ContextBudget: Budget-Aware Context Management for Long-Horizon Search Agents." arXiv:2604.01664.
7. Li, X., Lyu, T., et al. (2026). "Escaping the Context Bottleneck: Active Context Curation for LLM Agents via Reinforcement Learning." arXiv:2604.11462.
8. Zhu, Z., Liang, X., et al. (2026). "Don't Lose the Thread: Empowering Long-Horizon LLM Agents with Cognitive Resource Self-Allocation (CORAL)." OpenReview ICLR 2026.
9. Kang, M., Chen, W.-N., et al. (2025). "Acon: Optimizing Context Compression for Long-horizon LLM Agents." arXiv:2510.00615.
10. Lu, M., Sun, W., et al. (2025). "Scaling LLM Multi-turn RL with End-to-end Summarization-based Context Management (SUPO)." arXiv:2510.06727.
11. Anthropic. (2025). "Effective context engineering for AI agents." Anthropic Engineering Blog.
12. OpenAI. (2025). "Context Engineering — Short-Term Memory Management with Sessions." OpenAI Agents SDK Cookbook.
13. Lumer, E., Gulati, A., et al. (2025). "MemTool: Optimizing Short-Term Memory Management for Dynamic Tool Calling in LLM Agent Multi-Turn Conversations." arXiv:2507.21428.
14. Du, P. (2026). "Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers." arXiv:2603.07670.
15. Vstorm. "summarization-pydantic-ai README." https://github.com/vstorm-co/summarization-pydantic-ai
16. Vstorm. "Pydantic Deep Agents API — Agent." https://vstorm-co.github.io/pydantic-deepagents/api/agent/

---

*Research conducted via Exa AI Search API. PDFs fetched directly from arXiv. Total Exa cost: ~0.03 USD.*
