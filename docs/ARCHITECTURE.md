# Architecture

## Media Immutability Invariant

Once a piece of media — a video clip, narration audio, music track,
or rendered scene — has been created, it is immutable. Only two
operations on existing media are permitted: replace (swap it for a
freshly generated alternative, regenerating the entire slot) and
extend (append additional newly-generated media, for example a short
extension clip to fill remaining narration time).

The following are forbidden at every layer of the pipeline (recovery,
escalation, and assembly): looping (repeating existing media to fill
time), time-stretching (speeding up or slowing down existing media to
fit a target duration), and frozen frames in any form (holding a
single frame, or any static image derived from existing media, to
extend visible duration). The freeze prohibition applies whether the
hold occurs at the start, middle, or end of a clip and regardless of
the mechanism (decoder hold, still-image insert, last-frame repeat).
If a slot needs more visual duration, generate a new clip; do not
hold a frame.

## Escalation Pattern

Every pipeline operation is wrapped by the recovery middleware in
server/recovery.py. The intended design is a graduated ladder of
LLM-powered agents, one per rung, with increasing authority and
scope.

The ladder has five rungs. L0 is FIX: a domain specialist agent
rewrites inputs to repair the specific problem — for example, the
audio timing agent rewrites narration to fix duration overruns and
the visual prompt agent rewrites a visual prompt from QA feedback.
L1 is RETRY: an intelligent retry agent analyses error patterns and
adjusts parameters; it is not dumb exponential backoff. L2 is
CREATIVE: an alternative-strategy agent brainstorms a different
model or a different approach. L3 is COLLABORATIVE: an inter-agent
agent talks to other pipeline agents to coordinate a fix. L4 is
HUMAN: AG-UI escalation with the full diagnostic chain presented;
last resort.
