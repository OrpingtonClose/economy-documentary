# Segment Planning

## Segment-to-Paragraph Mapping & Clip Allocation

Frame options:
- 257 frames = 10.7s
- 105 frames = 4.4s  
- 73 frames = 3.0s

Rule: total clip duration >= TTS duration + 1s buffer
Breathing pauses: 1 clip of 105 frames (4.4s)

### ACT 1 (Warm amber, golden hour, firelight)

seg01 (26.6s) - Para 1: "Twenty percent...across America" → Need ≥27.6s → 3×257=32.1s ✓
breathing01 (4.4s) → 1×105=4.4s
seg02 (3.7s) - Para 2: "On March second...closed" → Need ≥4.7s → 1×105+1×73=7.4s (or 1×257=10.7s, too much) → better: 2×73=6.0s ✓
seg03 (44.4s) - Para 3: "Iranian drones...suspended" → Need ≥45.4s → 4×257+1×73=45.8s ✓ or 5×257=53.5s
seg04 (25.3s) - Para 4: "Within days...wall" → Need ≥26.3s → 3×257=32.1s ✓
seg05 (65.5s) - Para 5-6: "Now, an energy shock...chokepoint had closed" → Need ≥66.5s → 6×257+1×73=67.2s ✓ or 7×257=74.9s
seg06 (23.4s) - Para 7: "European gas storage...megawatt hour" → Need ≥24.4s → 3×257=32.1s ✓ or 2×257+1×105=25.8s ✓
seg07 (48.9s) - Para 8: "Germany was already...already underway" → Need ≥49.9s → 5×257=53.5s ✓
seg08 (43.0s) - Para 9: "But the damage...they are coming" → Need ≥44.0s → 4×257+1×73=45.8s ✓
seg09 (51.9s) - Para 10: "Meanwhile, the ECB...is trapped" → Need ≥52.9s → 5×257=53.5s ✓

### TRANSITION (warm to cold)

seg10_transition (20.0s) - Para 11: "The energy shock...feeding the others" → Need ≥21.0s → 2×257=21.4s ✓

### ACT 2 (Cold blue, grey, fluorescent, overcast)

seg11 (77.3s) - Para 12-13: "Start with oil...active again" → Need ≥78.3s → 7×257+1×105=79.3s ✓
breathing02 (4.4s) → 1×105=4.4s
seg12 (35.8s) - Para 14: "Turn to the stock market...persists" → Need ≥36.8s → 3×257+1×73=35.1s ✗ → 4×257=42.8s ✓ or 3×257+1×105=36.5s ✗ → 3×257+2×73=38.1s ✓
seg13 (57.5s) - Para 15: "But the risk...mark-to-fantasy" → Need ≥58.5s → 5×257+1×105=57.9s ✗ → 6×257=64.2s ✓ or 5×257+2×73=59.5s ✓
seg14 (52.6s) - Para 16-17: "When investors...behind the walls" → Need ≥53.6s → 5×257=53.5s ✗ → 5×257+1×73=56.5s ✓
breathing03 (4.4s) → 1×105=4.4s
seg15 (38.9s) - Para 18: "And this brings us...on its own" → Need ≥39.9s → 4×257=42.8s ✓
seg16 (34.2s) - Para 19: "For the first time...economically" → Need ≥35.2s → 3×257+1×105=36.5s ✓
seg17 (34.0s) - Para 20: "The labor market...in the background" → Need ≥35.0s → 3×257+1×105=36.5s ✓ or 3×257+2×73=38.1s
seg18 (28.2s) - Para 21: "That gap...buyers know it" → Need ≥29.2s → 3×257=32.1s ✓
seg19 (25.0s) - Para 22: "The foundation...policy answer" → Need ≥26.0s → 3×257=32.1s ✓

### TRANSITION (cold to warm)

seg20_transition (9.5s) - Para 23: "In an environment...directions" → Need ≥10.5s → 1×257=10.7s ✓

### ACT 3 (Warm gold returning, sunrise tones)

seg21 (56.6s) - Para 24: "The first is hard assets...barely begun" → Need ≥57.6s → 5×257+1×105=57.9s ✓ or 6×257=64.2s
breathing04 (4.4s) → 1×105=4.4s
seg22 (52.4s) - Para 25: "Silver has tripled...structural position" → Need ≥53.4s → 5×257=53.5s ✓
seg23 (20.8s) - Para 26: "The second direction...fourth quarter" → Need ≥21.8s → 2×257=21.4s ✗ → 2×257+1×73=24.4s ✓
seg24 (40.2s) - Para 27: "But beneath the price...to produce" → Need ≥41.2s → 4×257=42.8s ✓
breathing05 (4.4s) → 1×105=4.4s
seg25 (14.7s) - Para 28: "The rails are being built...showing strain" → Need ≥15.7s → 1×257+1×105=15.1s ✗ → 2×257=21.4s ✓ or 1×257+2×73=16.7s ✓
seg26_closing (42.2s) - Para 29-30: "An energy shock...recognize them early" → Need ≥43.2s → 4×257=42.8s ✗ → 4×257+1×73=45.8s ✓

## Total clips estimate: ~115-120 clips
