---
name: ltx-prompt-craft
description: How to write effective LTX-2.3 video generation prompts
---

# LTX-2.3 Prompt Craft

## 5-Layer Prompt Structure

Write every prompt as a SINGLE FLOWING PARAGRAPH (4-6 sentences) covering these elements in order:

### 1. Shot Size + Subject + Action
"A medium close-up of golden cloudberries glistening on a mossy bog as morning dew drips from their surfaces."

### 2. Environment + Atmosphere
"The scene takes place in a vast Finnish marshland at dawn, thin mist hovering above dark peat water."

### 3. Camera Movement
"The camera performs a slow dolly forward at low angle, gliding just above the moss surface."
Use ONE movement per shot — see cinematography skill.

### 4. Lighting + Style
"Lighting is soft golden-hour key with warm highlights and cool shadow fill. Shot on a 50mm lens, natural color."
Include realism anchors from visual_style (e.g. "4K", "raw footage").

### 5. Temporal Change
"Over time, the mist thins slightly as sunlight intensifies across the berries."
Describe what changes over the clip's duration.

## Use Present-Tense Verbs
"walks", "glows", "tilts" — NEVER past tense.

## LTX-2.3 Strengths (lean into these)
- Cinematic compositions with thoughtful lighting and shallow depth of field
- Single-subject emotional expressions and subtle gestures
- Atmosphere: fog, mist, golden-hour light, rain, reflections
- Clear camera language: "slow dolly in", "handheld tracking", "crane up"
- Stylized aesthetics when requested by visual_style

## LTX-2.3 Weaknesses (avoid entirely)
- Complex human figures in historical/period scenarios (causes cartoon look)
- Multiple characters interacting in the same frame
- Text, logos, or readable writing
- Complex physics or chaotic motion
- Overloaded scenes with too many subjects or actions

## Instead of Human Figures, Use
- Close-ups of objects, tools, artifacts, hands
- Landscapes and environments that evoke the era
- Macro details: textures, materials, surfaces
- Atmospheric establishing shots
- Animals, nature, weather

## Rules
- ONE subject, ONE action, ONE setting per prompt
- NO abstract concepts, infographics, split-screens, or text overlays
- NO human figures in complex historical scenarios
- Vary camera movements between consecutive phrases
