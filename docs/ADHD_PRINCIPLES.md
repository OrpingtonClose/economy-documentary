> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# ADHD-Friendly Documentary Principles

These principles guide every aspect of the pipeline — from script generation
to visual planning to final assembly.

## Core Rules

### 1. Maximum Scene Duration: 45 seconds
No single scene may exceed 45 seconds. This prevents cognitive fatigue and
maintains viewer engagement. The scenario evaluator enforces this.

### 2. Three Voices Per Scene
Every scene uses three distinct voice roles to create variety:
- **V1 — The Hook**: Grabs attention with surprising facts or provocative framing
- **V2 — The Expert**: Delivers authoritative analysis with data and mechanism
- **V3 — The Storyteller**: Provides human context, narrative, and emotional grounding

Voice rotation prevents monotony and matches natural attention patterns.

### 3. No Rhetorical Questions
Rhetorical questions ("So why doesn't the Fed just print more money?") are
banned. They feel performative and condescending. Instead, state the mechanism
directly: "The Fed's balance sheet expansion creates a specific inflation
transmission path."

### 4. Visual Variety Mandate
No two consecutive visual phrases may use the same:
- Camera style
- Environment
- LoRA style (unless narratively motivated)

The Coherence Evaluator enforces this during visual direction.

### 5. Dopamine Hooks
Each scene must include at least one "dopamine hook" — a surprising visual,
unexpected data point, or counterintuitive connection that rewards attention.

### 6. Content-Driven Visual Breakpoints
Visual transitions are driven by **content shifts** in the narration, not
arbitrary time slicing. The Content Analyst reads WhisperX alignment data
to identify when the narration changes topic, and visual phrases align to
those semantic boundaries.

## Anti-Patterns

- **Wall of text**: Long narration without visual change
- **Generic B-roll**: Stock footage that doesn't connect to narration
- **Lecture mode**: Single voice explaining for extended periods
- **Anxiety spiraling**: Building tension without resolution or mechanism
- **Smart-assy tone**: Clever narrative voice that talks down to viewer

## Quality Gates

The pipeline implements three human review gates:

1. **Gate 1 — Scenario Editor**: Review and edit the generated script
2. **Gate 2 — Prompt Reviewer**: Review visual prompts and LoRA selections
3. **Gate 3 — Clip Reviewer**: Review generated video clips with narration

Each gate allows approval, rejection, or regeneration of individual items.
