# ADHD-Optimized Documentary Production Principles
## Internalized from the Expanded Production Guide + Research

### Core Philosophy
The target audience has ADHD. Every visual, audio, and pacing decision must account for:
- **Shorter sustained attention windows** (but capable of deep hyperfocus when engaged)
- **Pattern-seeking brains** that thrive on rhythm and novelty
- **Sensitivity to both overstimulation AND understimulation**
- **Need for visual storytelling with human action**, not static establishing shots

---

## 1. PACING & SEGMENT LENGTH

### Segment Duration Rules
- **Core attention unit: 8-15 seconds** before a visual change is needed
- **Maximum static shot: 6 seconds** before ADHD viewers disengage
- **Scene transitions: every 15-30 seconds** with visual variety
- Longer clips MUST have internal motion, camera movement, or action to sustain attention
- **Pattern interrupts every 45-90 seconds**: a dramatic shift in visual style, angle, or energy

### Pacing Wave Pattern
- Don't maintain constant intensity — create waves
- HIGH intensity (action, drama, conflict) → MEDIUM (explanation, context) → LOW (reflection, beauty shot) → HIGH again
- Each wave cycle: ~2-3 minutes
- The narration already has natural pacing — clips must match its energy

### Attention Reset Techniques
- Sudden scale change (wide → extreme close-up)
- Color temperature shift (warm → cool or vice versa)
- Speed/motion change (fast action → slow contemplative)
- Environment change (indoor → outdoor, day → night)
- Human face close-up (faces are universal attention magnets)

---

## 2. VISUAL STORYTELLING (Critical for Prompts)

### Human Action Over Static Scenes
User explicitly wants: "scenes with human action, narrative sequences, visual storytelling — not static establishing shots"

**DO generate:**
- People in motion: walking, gesturing, working, reacting
- Hands interacting with objects (typing, signing documents, handling money)
- Facial expressions showing emotion (worry, determination, shock)
- Crowds in motion (protests, trading floors, market scenes)
- Dynamic interactions between people

**DON'T generate:**
- Empty cityscapes
- Still building exteriors
- Static landscape establishing shots
- Motionless objects on tables
- Generic skyline views

### Shot Composition for ADHD Audiences (from eye-tracking research)
- **Center-weighted framing**: ADHD viewers have more variable gaze patterns; keep subject center-frame
- **Leading lines**: Guide the eye to the subject (from Nature/PMC research on leading line composition)
- **Reduce visual clutter**: Fewer competing elements in frame
- **High contrast subjects**: Make the focal point obvious and distinct from background
- **Motion as attention anchor**: Moving subjects in a relatively still frame grab ADHD attention

### Visual Variety Requirements
For a 130-minute documentary, ensure across the 334 clips:
- Mix of scales: extreme wide, wide, medium, close-up, extreme close-up
- Mix of environments: indoor/outdoor, day/night, urban/rural/industrial
- Mix of subject types: individuals, crowds, objects, abstract/data visualizations
- No two consecutive clips should have the same shot scale + environment combination

---

## 3. PROMPT ENGINEERING FOR ADHD-ENGAGING CLIPS

### Prompt Structure for LTX-2.3
Based on successful experiments (béarnaise video, dialogue tests):
1. **Camera specification**: Shot type, angle, movement
2. **Subject action**: What's happening (MUST include motion/action)
3. **Environment details**: Setting, lighting, atmosphere
4. **Style markers**: Cinematic, documentary, dramatic lighting

### Action-Forward Prompt Patterns
Instead of: "A city skyline at sunset"
Write: "Aerial tracking shot following a convoy of military vehicles through abandoned city streets at dusk, dust clouds billowing, headlights cutting through amber haze, cinematic documentary style"

Instead of: "A stock exchange building"
Write: "Close-up tracking shot of a trader's hands frantically working multiple screens, fingers jabbing at keyboards, reflection of red numbers in his glasses, shallow depth of field, dramatic side lighting"

Instead of: "Oil refinery at night"
Write: "Slow dolly shot through rows of massive industrial pipes with workers in hardhats walking between them, steam venting from valves, harsh sodium lighting casting long shadows, industrial documentary cinematography"

### ADHD Engagement Boosters in Prompts
- **Specify camera movement** (tracking, dolly, crane, handheld) — motion = engagement
- **Include human presence** even in environmental shots (a figure in the landscape)
- **Describe lighting dramatically** (dramatic side lighting, rim light, chiaroscuro)
- **Add atmospheric elements** (smoke, dust, rain, steam, lens flare)
- **Specify emotional undertone** in body language when humans are present

---

## 4. AUDIO-VISUAL SYNC (Mayer's CTML Principles)

### Temporal Contiguity Principle
- Visuals must match narration timing precisely — this is already handled by clip timing
- When narration describes X, the visual should show X (not show Y while talking about X)

### Coherence Principle
- Every visual element must serve the narrative — no decorative filler
- Extraneous visual complexity hurts comprehension, especially for ADHD viewers

### Segmenting Principle
- Complex information broken into digestible chunks
- Each clip = one concept/moment/beat
- Natural pauses between segments (already built into narration)

### Signaling Principle
- Visual cues that match narrative emphasis
- Close-ups when narration hits key points
- Wide shots for context/transitions

---

## 5. MUSIC & SOUND CONSIDERATIONS

### Research-Backed Findings (from Communications Biology, 2024)
- **60-90 BPM optimal** for sustained attention in ADHD viewers
- **Beta-range amplitude modulation (12-20 Hz)** in music specifically benefits ADHD attention
- **Phase-locking**: Brain entrains to rhythmic auditory stimuli — consistent musical rhythm helps
- **Background music as double-edged sword**: Can be stimulation boost OR distraction for ADHD
- Keep music subordinate to narration — never competing

### Note: Audio is LOCKED
- v5_narration.wav is final and must not be touched
- Music/sound design decisions are separate from clip generation
- But clip visuals should anticipate the emotional arc of the narration

---

## 6. ANTI-PATTERNS TO AVOID

### For ADHD Audiences, NEVER:
- Generate the same visual style for 3+ consecutive clips
- Use slow-motion on already-slow content (boring becomes unwatchable)
- Create clips that are just "establishing" without action
- Generate text-heavy scenes (user rule: "don't try to generate letters on screen")
- Use uniform pacing throughout — must have rhythm variation
- Repeat clips or loop content (user rule: "all clips will not be repeated")
- Create overly abstract/conceptual visuals without grounding them in human experience
- Let any clip be purely decorative without narrative purpose

### Video Must Not:
- Be stretched under any circumstance
- Be looped or repeated upon deployment
- Use distillation, FP8, or upscalers
- Be shorter than narration duration (regenerate if too short)

---

## 7. PRODUCTION QUALITY STANDARDS

### Per User Requirements:
- Full bf16 quality (no quantization)
- Single-stage pipeline (TI2VidOneStagePipeline)
- Resolution: Native LTX-2.3 output resolution
- Frame rate: 24fps target for cinematic feel
- Clips generated slightly longer than needed, then trimmed (never stretched)
- Last-frame conditioning for continuity between sequential clips where desired
- All clips uploaded to B2 (economy-vid-assets/v5_clips_v2/)

### Quality Hierarchy:
1. Clip matches narration content and timing
2. Clip has engaging human action / visual storytelling
3. Clip has dynamic camera movement
4. Clip has dramatic lighting/atmosphere
5. Clip maintains visual continuity with adjacent clips
