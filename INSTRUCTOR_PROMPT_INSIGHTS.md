# Instructor Prompt Engineering Insights

## What Works

### 1. Chain-of-Thought Parsing

Adding a `chain_of_thought` field to the extraction model improves accuracy by ~10%.

```python
class MultiEffect(BaseModel):
    chain_of_thought: str = Field(
        description="Step-by-step reasoning: what did you find?"
    )
    effects: list[SingleEffect]
    confidence: int = Field(ge=0, le=10)
```

**Why it works:** The model reasons aloud before committing to structured output.

### 2. Field-Level Examples in Docstrings

Few-shot examples in the model docstring guide the model better than system prompt examples.

```python
class MultiEffect(BaseModel):
    """
    Extract effects from agent text.

    Examples:
        Input: "Narration V1: 'Rainbows are illusions.'"
        → effects: [{effect_type: "UpdateScript", narration_v1: "Rainbows are illusions."}]

        Input: "NoOp: waiting"
        → effects: [{effect_type: "NoOp", noop_reason: "waiting"}]
    """
```

**Why it works:** Instructor injects docstring examples into the JSON schema sent to the model.

### 3. Reask Validation (max_retries)

Set `max_retries=3` on the instructor client. Failed validation sends error context back to the model.

```python
client.chat.completions.create(
    model="deepseek-v4-flash",
    response_model=MultiEffect,
    messages=[...],
    max_retries=3,
)
```

**Behind the scenes:**
```python
except ValidationError as e:
    kwargs["messages"].append({
        "role": "user",
        "content": f"Please correct; errors: {e}",
    })
```

### 4. Literal Types for Classification

Use `Literal` for effect_type instead of `str`. This constrains the model to valid values.

```python
effect_type: Literal["UpdateScript", "GenerateNarrationAudio", "NoOp"]
```

**Why it works:** The JSON schema enforces enum constraints. The model literally cannot output invalid values.

### 5. Self-Describing Fields

Every field needs a clear `description`. The description becomes part of the prompt.

```python
narration_v1: str = Field(
    default="",
    description="Primary narration text - complete sentences, not labels"
)
```

**Why it works:** Instructor builds the system prompt from field descriptions.

## What Doesn't Work

### 1. Over-Structured Output from Agents

Forcing agents to output strict JSON or YAML causes resistance. Agents output markdown prose.

**Solution:** Parse free text with instructor instead of forcing structure.

### 2. Complex Nested Prompts

Agents ignore long, complex system prompts. Keep instructions short.

**Solution:** 3-5 sentences max. Let the parser handle complexity.

### 3. Negative Constraints Alone

"NEVER use markdown" is ignored. Agents need positive examples.

**Solution:** Show what TO do, not what NOT to do.

## Best Practices for Pipeline Parsing

### Step 1: Define the Extraction Model

```python
class ExtractedEffect(BaseModel):
    """One effect from agent text.

    Example:
        Input: "Create audio for V1: 'Hello world'"
        → effect_type: "GenerateNarrationAudio"
        → voice: "V1"
        → text: "Hello world"
    """
    effect_type: Literal[...] = Field(description="...")
    # ... fields
```

### Step 2: Add Chain-of-Thought

```python
class ParseResult(BaseModel):
    chain_of_thought: str = Field(
        description="Think step by step. What did the agent say? What effects are present?"
    )
    effects: list[ExtractedEffect]
```

### Step 3: Configure Client

```python
client = instructor.from_openai(
    OpenAI(api_key=..., base_url="..."),
    mode=instructor.Mode.JSON,
)
```

### Step 4: Extract with Retries

```python
result = client.chat.completions.create(
    model="deepseek-v4-flash",
    response_model=ParseResult,
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": agent_text},
    ],
    max_retries=3,
    temperature=0.0,
)
```

### Step 5: Validate Effects

```python
for effect in result.effects:
    if effect.effect_type == "UpdateScript" and not effect.narration_v1:
        raise ValueError("UpdateScript missing narration_v1")
```

## System Prompt Template

```python
_SYSTEM_PROMPT = """You are an expert document parser for a documentary pipeline.

Your job: read free-form agent text and extract structured effects.

STEP-BY-STEP:
1. Read the entire text carefully.
2. Identify what the agent is trying to do.
3. Extract ALL structured data you can find.
4. If no actionable data exists, return empty effects list.
5. Rate your confidence (0-10).

EFFECT TYPES:
- UpdateScript: Script changes. Extract narration_v1, narration_v2, narration_v3, visual_notes, dopamine_hook, pronunciation_hints, duration_sec, scene_num.
- GenerateNarrationAudio: TTS request. Extract voice, text, scene_num.
- RenderVideoSegment: Video render. Extract prompt, lora_id, duration_sec, scene_num.
- MergeIntoOTIO: Merge clips. Extract audio_clips, video_clips.
- ExecuteRawBash: Bash command. Extract command, reason.
- NoOp: No action. Use ONLY if genuinely nothing found.

RULES:
- NEVER hallucinate. If text is just chatting, return empty effects.
- Extract actual content, not labels. "Narration V1" is a label; the quoted text after it is the content.
- One GenerateNarrationAudio per voice per scene.
- One RenderVideoSegment per scene.
- If text says "NoOp" or "waiting", return empty effects or single NoOp.
- Be conservative. Low confidence → fewer effects.
"""
```

## DeepSeek-Specific Notes

- Model name: `deepseek-v4-flash` (not `deepseek-chat`)
- Base URL: `https://api.deepseek.com/v1`
- Mode: `instructor.Mode.JSON` (TOOLS mode causes issues with DeepSeek)
- Temperature: 0.0 for deterministic extraction
- max_retries: 3 is sufficient

## Cost Optimization

- Use `deepseek-v4-flash` for extraction (fast, cheap)
- Use `deepseek-reasoner` for agent reasoning (slow, expensive)
- Extraction is called on every agent output → keep it cheap
- Reasoning is done once per agent → can afford better model

## References

- Instructor docs: https://python.useinstructor.com/
- Prompting guide: https://python.useinstructor.com/prompting/
- Validation and reask: https://python.useinstructor.com/concepts/reask_validation/
- The Prompt Report: https://learnprompting.org/
