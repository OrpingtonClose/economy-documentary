# Playground live-feedback test plan (PR #343)

Target URL: http://142.171.48.138:29561/components (staging Vast.ai VM)

## What changed (user-visible)

Three complaints from the user, three fixes to prove:

1. **"no smooth streaming of anxiety calming messages"** — narrator backend
   was snapping a full sentence every 2.5s. Now it emits every 1.5s with a
   3s silent-report fallback, and the frontend reveals the text one
   character at a time via `useTypewriter`. Evidence:
   `server/strands_agents/playground/narrator.py:44-220`,
   `frontend-playground/src/app/components/[id]/ComponentWorkbench.tsx:362-478`.

2. **"UI jumps with the size of the text unpredictably"** — status line had
   no fixed height, interpretation card was conditionally rendered (0px →
   ~150px), result panels collapsed to nothing when empty. Now:
   status is a fixed 44px rail (`h-11` at `ComponentWorkbench.tsx:455`),
   interpretation always renders with `min-h-[128px]`
   (`ComponentWorkbench.tsx:511`), result panels use `.pg-stable-surface`
   (`globals.css:38`).

3. **"if I initiated a process and nothing is happening, it must be emitted
   as such"** — narrator re-emits during ≥3s silence with advancing
   elapsed framing (e.g. "still on openai/gpt-4o-mini (9.0s elapsed)").
   Verified live on staging: SSE tail showed 10+ narrate events over
   30s, counter advancing 1.5s → 3.0s → 4.5s → … → 16.5s.

## Primary flow

**Run c01 scenario_agent / economics_basics on staging** — this is a live
LLM run (Gemini 3 Pro preview or OpenAI gpt-4o), scenario generation
takes 20–40s. This is the only flow that exercises all three fixes at
once: the long LLM silence window forces the narrator to keep emitting,
the typewriter gets time to reveal each line, and the interpretation
card lands at the end without shoving the page.

### Steps (GUI, no setup)

1. Navigate to `http://142.171.48.138:29561/components`.
   - **Pass:** grid of 15 component cards visible.
   - **Fail:** blank page, 502, or grid smaller than 15.

2. Click the card `c01 · scenario_agent`.
   - **Pass:** workbench loads. Status rail visible under the case list
     but **empty text** (the idle state — silence when nothing has
     been asked is the point). Interpretation card visible with
     placeholder text `run a case to see a one-paragraph
     interpretation here` (italic, dashed border).
   - **Fail:** status rail shows "Ready" or any text in idle; or
     interpretation card is not visible (would mean layout will jump
     later).

3. Record the pixel-Y of the top of `RawEventLog` region (take a
   screenshot and eyeball against a reference point — e.g. the
   "Interpretation" heading). This is the **layout-anchor baseline**.

4. Click `Run`.
   - **Pass within 1s:** status rail turns green with a pulsing dot
     and renders the word "dispatching…" character-by-character
     (visibly shorter at t+100ms than at t+500ms). Status rail
     vertical position unchanged. Interpretation card still visible,
     still 128px min-height, still at same Y.
   - **Fail:** status rail jumps in size, interpretation card
     disappears, page shifts down by more than ~2px.

5. During the 20–40s LLM thinking window, take 3 screenshots at
   ~5s intervals (t+5s, t+15s, t+25s after click). For each, record
   the narration line text.
   - **Pass:** every screenshot shows a **different** line, with an
     elapsed-time counter that **increases** across the three
     screenshots (e.g. "probing … (4.5s elapsed)" → "still on … (10.5s
     elapsed)" → "still on … (22.5s elapsed)"). Counter must not be
     stuck at one value.
   - **Fail:** identical text across any two screenshots, or the line
     becomes blank/frozen during the thinking window, or the counter
     stops advancing.

6. While the run is in flight (any of the screenshots above), verify
   the layout-anchor:
   - **Pass:** the top-edge of the RawEventLog disclosure (`Raw event
     log (N)`) is at the same pixel-Y across all three screenshots
     (±2px for sub-pixel rendering).
   - **Fail:** RawEventLog row Y shifts by more than 4px between
     any two screenshots.

7. Wait for terminal. Status rail transitions to grey (`bg-pg-surface
   text-pg-muted`), interpretation card wrapper transitions from
   dashed border to solid accent-colored border, and the
   interpretation paragraph reveals character-by-character.
   - **Pass:** observable typewriter reveal of the interpretation
     paragraph (visibly shorter at t+200ms than t+1s). Status chip
     in interpretation header shows `OK` or `MODEL_UNREACHABLE` or
     `TASK_ERROR` — whichever the terminal produced.
   - **Fail:** full paragraph snaps in at once, or interpretation
     card remains empty with no status chip, or the interpretation
     card's Y position changes when it transitions from placeholder
     to populated state.

8. Verify raw event log (disclosure):
   - Click the `▸ Raw event log (N)` summary.
   - **Pass:** expands to show an ordered list with at least 10 rows
     including `run.dispatched`, `probe.done`, `task.start`,
     several `narrate` rows (one per 1.5s of run time), `run.ok`
     (or `run.error`), `interpret`. Seq numbers are monotonically
     increasing.
   - **Fail:** fewer than 3 narrate rows (would mean narrator not
     emitting continuously), or no `interpret` row, or seq numbers
     not monotonic.

## Secondary flow — fast terminal (c02)

Purpose: verify idle → dispatch → OK transition doesn't jump the
layout even when the run finishes in < 1s (no narration window at
all). The status rail must still remain a stable 44px rail.

1. Back out to `/components`, click `c02 · intent_exact`.
2. Select case `intent_exact` (first), click `Run`.
   - **Pass:** status rail briefly shows "dispatching…" then the final
     narration line or `run.ok` summary, then transitions to grey
     terminal state, all within ~2s. No layout jump of anchor
     element. Interpretation card populates with a paragraph and
     status chip shows `OK`.
   - **Fail:** status rail resizes, interpretation card lands with a
     jump, or dispatch → terminal transition causes the output JSON
     panel to shift down.

## Recording & reporting

- Maximize browser window before recording.
- Single continuous recording covering the two flows above.
- Annotate: `test_start` at step 4 (c01 run), `assertion` at steps
  5/6/7, `test_start` at secondary flow, `assertion` at terminal.
- Do NOT test: auth flows (no login), evaluator correctness (covered
  by the evaluator endpoint tests), save-as-case (not what the user
  asked for).

## Why these tests distinguish working from broken

- Step 4 distinguishes typewriter vs snap-in by requiring visibly
  shorter text at t+100ms than t+500ms — a broken (snap-in)
  implementation would show full text at both.
- Step 5 distinguishes continuous-emission vs stalled by requiring
  three distinct lines with advancing counters — a broken (frozen
  during silence) implementation would show identical text.
- Step 6 distinguishes fixed-rail vs variable-height by requiring
  the anchor Y to stay stable — a broken (no min-height)
  implementation would shift the anchor when the interpretation
  card lands.
- Step 7 distinguishes character-reveal vs snap-in for the
  interpretation — a broken implementation would show the full
  paragraph at once.
- Step 8 distinguishes 1.5s-cadence narrator vs the old 2.5s one
  by requiring at least 3 narrate rows in a 20–40s window (the old
  code would produce ≤1 or 2 in the silence windows).
