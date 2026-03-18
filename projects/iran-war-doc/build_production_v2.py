#!/usr/bin/env python3
"""
Parse SCENARIO_V2.MD and build:
1. Master OTIO timeline with all tracks
2. Per-scene narration .txt files for Qwen3-TTS
3. Per-clip LTX-2.3 video generation prompts
4. Production manifest JSON
"""

import json
import re
import os
import opentimelineio as otio

SCENARIO_PATH = "/home/user/workspace/iran-war-doc/SCENARIO_V2.MD"
OUTPUT_DIR = "/home/user/workspace/iran-war-doc/production"
NARRATION_DIR = os.path.join(OUTPUT_DIR, "narration_scripts")
PROMPTS_DIR = os.path.join(OUTPUT_DIR, "video_prompts")
OTIO_PATH = os.path.join(OUTPUT_DIR, "war_economy_master.otio")
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "production_manifest.json")

for d in [OUTPUT_DIR, NARRATION_DIR, PROMPTS_DIR]:
    os.makedirs(d, exist_ok=True)

# Clean old files
for d in [NARRATION_DIR, PROMPTS_DIR]:
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))

with open(SCENARIO_PATH, "r") as f:
    content = f.read()

# ============================================================
# STEP 1: Parse the Scenario
# ============================================================

VOICE_NAMES = {
    "1": "Financial Journalist",
    "2": "Intelligence/Economic Analyst",
    "3": "Historian"
}

# Find all scene blocks by splitting on scene headers
scene_starts = list(re.finditer(r'^### SCENE (\d+):', content, re.MULTILINE))
scenes = []

for i, match in enumerate(scene_starts):
    scene_num = int(match.group(1))
    start = match.start()
    end = scene_starts[i + 1].start() if i + 1 < len(scene_starts) else len(content)
    block = content[start:end]

    # Title
    title_match = re.search(r'### SCENE \d+: (.+)', block)
    title = title_match.group(1).strip() if title_match else f"Scene {scene_num}"

    # Duration
    dur_match = re.search(r'\*\*Duration:\*\* (\d+) seconds', block)
    duration_sec = int(dur_match.group(1)) if dur_match else 120

    # Narration section
    narration_match = re.search(r'#### NARRATION\n(.*?)(?=\n#### [A-Z])', block, re.DOTALL)
    narration_text = narration_match.group(1).strip() if narration_match else ""

    # Visual description
    visual_match = re.search(r'#### VISUAL DESCRIPTION\n(.*?)(?=\n#### [A-Z])', block, re.DOTALL)
    visual_text = visual_match.group(1).strip() if visual_match else ""

    # Causal beats
    beats_match = re.search(r'#### CAUSAL BEATS\n(.*?)(?=\n#### [A-Z])', block, re.DOTALL)
    beats_text = beats_match.group(1).strip() if beats_match else ""

    # Production notes
    notes_match = re.search(r'#### PRODUCTION NOTES\n(.*?)(?=\n---|\Z)', block, re.DOTALL)
    notes_text = notes_match.group(1).strip() if notes_match else ""

    # --- Parse narration into voice segments ---
    voice_segments = []
    # Split on voice markers: **V1:** or **V1 (Role):**
    parts = re.split(r'(\*\*V[123](?:\s*\([^)]*\))?:\*\*)', narration_text)

    current_voice = None
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Check if this part is a voice marker
        vm = re.match(r'\*\*V([123])(?:\s*\([^)]*\))?:\*\*', part)
        if vm:
            current_voice = vm.group(1)
            continue

        if current_voice:
            # This is the text for the current voice
            text = part.strip()
            # Remove [pause] markers
            text = re.sub(r'\[pause\]', '', text).strip()
            # Remove stage directions in italics
            text = re.sub(r'^\*\(.*?\)\*\s*', '', text).strip()
            text = re.sub(r'^\*[^*]+\*\s*$', '', text, flags=re.MULTILINE).strip()
            # Clean quotes
            text = text.strip('"').strip('\u201c').strip('\u201d').strip()
            # Remove empty lines
            text = re.sub(r'\n\s*\n', ' ', text).strip()

            if text and len(text) > 10:
                voice_segments.append({
                    "voice": f"V{current_voice}",
                    "voice_name": VOICE_NAMES.get(current_voice, "Unknown"),
                    "text": text
                })
            current_voice = None  # Reset after consuming text

    # --- Parse causal beats into clips ---
    clips = []
    beat_lines = re.findall(r'\| (\d+:\d+) \| (.+?) \| (.+?) \|', beats_text)
    for j, (timestamp, narration_trigger, visual_action) in enumerate(beat_lines):
        parts_t = timestamp.split(":")
        beat_sec = int(parts_t[0]) * 60 + int(parts_t[1])

        if j + 1 < len(beat_lines):
            next_parts = beat_lines[j + 1][0].split(":")
            next_sec = int(next_parts[0]) * 60 + int(next_parts[1])
            clip_dur = next_sec - beat_sec
        else:
            clip_dur = duration_sec - beat_sec

        clips.append({
            "clip_index": j,
            "start_sec": beat_sec,
            "duration_sec": max(clip_dur, 5),
            "narration_trigger": narration_trigger.strip(),
            "visual_action": visual_action.strip()
        })

    # Extract color palette
    palette = list(dict.fromkeys(re.findall(r'#[0-9A-Fa-f]{6}', notes_text)))

    # Camera
    camera_match = re.search(r'\*\*Camera:\*\*\s*(.+?)(?:\n|$)', notes_text)
    camera = camera_match.group(1).strip() if camera_match else ""

    # Transition
    transition_match = re.search(r'\*\*Transition Out:\*\*\s*(.+?)(?:\n|$)', notes_text)
    transition = transition_match.group(1).strip() if transition_match else "Hard cut"

    scenes.append({
        "number": scene_num,
        "title": title,
        "duration_sec": duration_sec,
        "narration_raw": narration_text,
        "visual_description": visual_text,
        "voice_segments": voice_segments,
        "clips": clips,
        "color_palette": palette[:6],
        "camera": camera,
        "transition_out": transition,
        "production_notes": notes_text
    })

print(f"Parsed {len(scenes)} scenes")
total_dur = sum(s["duration_sec"] for s in scenes)
print(f"Total duration: {total_dur}s ({total_dur/60:.1f} min)")
total_clips = sum(len(s["clips"]) for s in scenes)
print(f"Total video clips: {total_clips}")
total_voice_segments = sum(len(s["voice_segments"]) for s in scenes)
print(f"Total voice segments: {total_voice_segments}")
total_words = sum(len(s["text"].split()) for scene in scenes for s in scene["voice_segments"])
print(f"Total narration words: {total_words} (~{total_words/150:.0f} min at 150wpm)")

# ============================================================
# STEP 2: Generate Narration Scripts
# ============================================================
print("\n=== GENERATING NARRATION SCRIPTS ===")

for scene in scenes:
    scene_id = f"scene_{scene['number']:02d}"

    # Combined narration
    narration_file = os.path.join(NARRATION_DIR, f"{scene_id}_narration.txt")
    parts = []
    for seg in scene["voice_segments"]:
        parts.append(f"[{seg['voice']}] {seg['text']}")
    with open(narration_file, "w") as f:
        f.write("\n\n".join(parts))

    # Individual segment files
    for idx, seg in enumerate(scene["voice_segments"]):
        seg_file = os.path.join(NARRATION_DIR, f"{scene_id}_seg{idx:02d}_{seg['voice'].lower()}.txt")
        with open(seg_file, "w") as f:
            f.write(seg["text"])

    print(f"  Scene {scene['number']:2d} ({scene['title'][:35]:35s}): {len(scene['voice_segments'])} segments, {sum(len(s['text'].split()) for s in scene['voice_segments'])} words")

narr_file_count = len(os.listdir(NARRATION_DIR))
print(f"Total narration files: {narr_file_count}")

# ============================================================
# STEP 3: Generate Video Prompts
# ============================================================
print("\n=== GENERATING VIDEO PROMPTS ===")

STYLE_STRING = (
    "cinematic documentary, photorealistic, shot on Arri Alexa, "
    "16:9 widescreen, shallow depth of field, anamorphic lens flare, "
    "dramatic lighting, high contrast, subtle film grain"
)

all_video_prompts = []

for scene in scenes:
    scene_id = f"scene_{scene['number']:02d}"

    for clip in scene["clips"]:
        clip_id = f"{scene_id}_clip{clip['clip_index']:02d}"
        va = clip["visual_action"]

        # Determine visual world
        ledger_kw = ["trading", "bloomberg", "terminal", "boardroom", "white house",
                     "conference", "office", "screen", "server", "vault", "institutional",
                     "briefing", "suited", "desk", "corporate", "wall street", "digital",
                     "numbers", "display", "monitor"]
        street_kw = ["gas station", "family", "kitchen", "grocery", "street", "crowd",
                     "protest", "port", "shipping", "worker", "consumer", "pump",
                     "highway", "store", "market", "refugee", "neighborhood", "apartment"]

        is_ledger = any(kw in va.lower() for kw in ledger_kw)
        is_street = any(kw in va.lower() for kw in street_kw)

        if is_ledger:
            mood = "cold blue-white institutional lighting, steel and glass surfaces, corporate atmosphere"
        elif is_street:
            mood = "warm amber light fading to harsh fluorescent, lived-in textures, human scale"
        else:
            mood = "dramatic cinematic lighting, high contrast shadows"

        palette_str = ""
        if scene["color_palette"]:
            palette_str = f", color palette: {', '.join(scene['color_palette'][:4])}"

        camera_str = scene["camera"] if scene["camera"] else "slow dolly movement"

        prompt = f"{va}. {mood}. {camera_str}. {STYLE_STRING}{palette_str}"

        clip_duration = clip["duration_sec"]
        # LTX-2.3 generates ~5s clips at 24fps (121 frames)
        ltx_clip_count = max(1, (clip_duration + 4) // 5)

        clip_data = {
            "clip_id": clip_id,
            "scene_number": scene["number"],
            "scene_title": scene["title"],
            "clip_index": clip["clip_index"],
            "start_sec_in_scene": clip["start_sec"],
            "target_duration_sec": clip_duration,
            "ltx_clips_needed": ltx_clip_count,
            "prompt": prompt,
            "visual_action_raw": va,
            "narration_trigger": clip["narration_trigger"],
            "world": "ledger" if is_ledger else ("street" if is_street else "neutral"),
            "color_palette": scene["color_palette"][:4],
            "transition_to_next": "cut"
        }

        if clip["clip_index"] == len(scene["clips"]) - 1:
            clip_data["transition_to_next"] = scene["transition_out"]

        all_video_prompts.append(clip_data)

        prompt_file = os.path.join(PROMPTS_DIR, f"{clip_id}.json")
        with open(prompt_file, "w") as f:
            json.dump(clip_data, f, indent=2)

    ltx_total = sum(1 for p in all_video_prompts if p["scene_number"] == scene["number"])
    ltx_gens = sum(p["ltx_clips_needed"] for p in all_video_prompts if p["scene_number"] == scene["number"])
    print(f"  Scene {scene['number']:2d}: {ltx_total} clips, {ltx_gens} LTX generations")

print(f"\nTotal video clips: {len(all_video_prompts)}")
print(f"Total LTX generations needed: {sum(c['ltx_clips_needed'] for c in all_video_prompts)}")

# ============================================================
# STEP 4: Build OTIO Timeline
# ============================================================
print("\n=== BUILDING OTIO TIMELINE ===")

timeline = otio.schema.Timeline(name="THE WAR ECONOMY: Who Profits When Missiles Fly")
timeline.global_start_time = otio.opentime.RationalTime(0, 24)

video_track = otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video)
narration_track = otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio)
music_track = otio.schema.Track(name="A2_Music", kind=otio.schema.TrackKind.Audio)
ambient_track = otio.schema.Track(name="A3_Ambient", kind=otio.schema.TrackKind.Audio)

cumulative_sec = 0

for scene in scenes:
    scene_id = f"scene_{scene['number']:02d}"
    scene_dur_frames = scene["duration_sec"] * 24

    # VIDEO TRACK
    scene_clips = [p for p in all_video_prompts if p["scene_number"] == scene["number"]]
    for ci, clip_data in enumerate(scene_clips):
        clip_dur_frames = clip_data["target_duration_sec"] * 24

        media_ref = otio.schema.ExternalReference(
            target_url=f"./clips/{clip_data['clip_id']}.mp4",
            available_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(clip_dur_frames, 24)
            )
        )

        video_clip = otio.schema.Clip(
            name=clip_data["clip_id"],
            media_reference=media_ref,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(clip_dur_frames, 24)
            )
        )
        video_clip.metadata["scene"] = scene["number"]
        video_clip.metadata["world"] = clip_data["world"]
        video_clip.metadata["visual_action"] = clip_data["visual_action_raw"]

        # Crossfade within scene (0.5s dissolve)
        if ci > 0:
            xfade = otio.schema.Transition(
                name=f"{clip_data['clip_id']}_xfade",
                transition_type="SMPTE_Dissolve",
                in_offset=otio.opentime.RationalTime(6, 24),
                out_offset=otio.opentime.RationalTime(6, 24)
            )
            video_track.append(xfade)

        video_track.append(video_clip)

    # Inter-scene transition (1s fade through black between acts, 0.5s dissolve within acts)
    # Determine if next scene is a new act
    act_boundaries = {5, 8, 12, 15, 18, 22}
    if scene["number"] + 1 in act_boundaries:
        # Fade to black between acts: 1s gap
        gap = otio.schema.Gap(
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(24, 24)
            )
        )
        gap.name = f"{scene_id}_act_break"
        video_track.append(gap)

    # NARRATION TRACK
    for seg_idx, seg in enumerate(scene["voice_segments"]):
        seg_id = f"{scene_id}_seg{seg_idx:02d}_{seg['voice'].lower()}"
        word_count = len(seg["text"].split())
        est_duration_sec = max(3, word_count / 2.5)
        est_dur_frames = int(est_duration_sec * 24)

        media_ref = otio.schema.ExternalReference(
            target_url=f"./narration/{seg_id}.wav",
            available_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(est_dur_frames, 24)
            )
        )

        narr_clip = otio.schema.Clip(
            name=seg_id,
            media_reference=media_ref,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(est_dur_frames, 24)
            )
        )
        narr_clip.metadata["voice"] = seg["voice"]
        narr_clip.metadata["word_count"] = word_count
        narr_clip.metadata["est_sec"] = round(est_duration_sec, 1)
        narration_track.append(narr_clip)

        # Pause between segments
        if seg_idx < len(scene["voice_segments"]) - 1:
            pause = otio.schema.Gap(
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(18, 24)  # 0.75s
                )
            )
            pause.name = f"{scene_id}_pause_{seg_idx}"
            narration_track.append(pause)

    # Inter-scene narration gap
    inter = otio.schema.Gap(
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(36, 24)  # 1.5s
        )
    )
    inter.name = f"{scene_id}_scene_gap"
    narration_track.append(inter)

    # MUSIC: placeholder
    music_clip = otio.schema.Clip(
        name=f"{scene_id}_music",
        media_reference=otio.schema.MissingReference(),
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(scene_dur_frames, 24)
        )
    )
    music_track.append(music_clip)

    # AMBIENT: placeholder
    ambient_clip = otio.schema.Clip(
        name=f"{scene_id}_ambient",
        media_reference=otio.schema.MissingReference(),
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(scene_dur_frames, 24)
        )
    )
    ambient_track.append(ambient_clip)

    cumulative_sec += scene["duration_sec"]

timeline.tracks.append(video_track)
timeline.tracks.append(narration_track)
timeline.tracks.append(music_track)
timeline.tracks.append(ambient_track)

otio.adapters.write_to_file(timeline, OTIO_PATH)

# Stats
v_dur = sum(c.source_range.duration.to_seconds() for c in video_track if isinstance(c, otio.schema.Clip))
n_dur = sum(c.source_range.duration.to_seconds() for c in narration_track if isinstance(c, otio.schema.Clip))
print(f"OTIO saved: {OTIO_PATH}")
print(f"Video track: {v_dur:.1f}s ({v_dur/60:.1f} min), {sum(1 for c in video_track if isinstance(c, otio.schema.Clip))} clips")
print(f"Narration track: {n_dur:.1f}s ({n_dur/60:.1f} min), {sum(1 for c in narration_track if isinstance(c, otio.schema.Clip))} segments")
print(f"Tracks: {len(timeline.tracks)}")

# ============================================================
# STEP 5: Build Production Manifest
# ============================================================
print("\n=== BUILDING PRODUCTION MANIFEST ===")

act_defs = {
    1: (1, 4, "THE PRICE TAG"),
    2: (5, 7, "THE SMART MONEY"),
    3: (8, 11, "THE CRYPTO PIPELINE"),
    4: (12, 14, "THE RUSSIA DEAL"),
    5: (15, 17, "BILLIONAIRE'S ROW"),
    6: (18, 21, "THE DEFENSE PAYDAY"),
    7: (22, 26, "THE BILL")
}

manifest = {
    "title": "THE WAR ECONOMY: Who Profits When Missiles Fly",
    "version": "v2",
    "target_runtime_sec": total_dur,
    "target_runtime_min": round(total_dur / 60, 1),
    "fps": 24,
    "resolution": "1280x720",
    "aspect_ratio": "16:9",
    "otio_file": "production/war_economy_master.otio",
    "total_scenes": len(scenes),
    "total_video_clips": len(all_video_prompts),
    "total_ltx_generations": sum(c["ltx_clips_needed"] for c in all_video_prompts),
    "total_narration_segments": total_voice_segments,
    "total_narration_words": total_words,
    "estimated_narration_min": round(total_words / 150, 1),
    "production_system": {
        "video_model": "LTX-2.3",
        "video_settings": {
            "precision": "bf16",
            "distillation": False,
            "fp8": False,
            "upscaler": False,
            "fps": 24,
            "resolution": "native LTX-2.3",
            "frame_chain": "last-frame conditioning for clips > 5s"
        },
        "audio_model": "Qwen3-TTS",
        "narration_voices": {
            "V1": "Financial Journalist — sharp, data-driven, investigative",
            "V2": "Intelligence/Economic Analyst — precise, measured, analytical",
            "V3": "Historian — contemplative, authoritative, wise"
        }
    },
    "constraints": {
        "no_text_on_screen": True,
        "no_looping": True,
        "no_stretching": True,
        "no_distillation": True,
        "no_fp8": True,
        "no_upscalers": True,
        "adhd_pacing": "8-15s attention units, 6s max static shot, pattern interrupt every 45-90s"
    },
    "acts": [],
    "scenes": []
}

for act_num, (s_start, s_end, act_title) in act_defs.items():
    act_scenes = [s for s in scenes if s_start <= s["number"] <= s_end]
    act_dur = sum(s["duration_sec"] for s in act_scenes)
    act_clips = sum(len(s["clips"]) for s in act_scenes)
    act_words = sum(len(seg["text"].split()) for s in act_scenes for seg in s["voice_segments"])
    manifest["acts"].append({
        "act": act_num,
        "title": act_title,
        "scenes": list(range(s_start, s_end + 1)),
        "duration_sec": act_dur,
        "duration_min": round(act_dur / 60, 1),
        "clip_count": act_clips,
        "narration_words": act_words
    })

for scene in scenes:
    sc_prompts = [p for p in all_video_prompts if p["scene_number"] == scene["number"]]
    manifest["scenes"].append({
        "number": scene["number"],
        "title": scene["title"],
        "duration_sec": scene["duration_sec"],
        "clip_count": len(scene["clips"]),
        "ltx_generations": sum(p["ltx_clips_needed"] for p in sc_prompts),
        "voice_segment_count": len(scene["voice_segments"]),
        "narration_words": sum(len(s["text"].split()) for s in scene["voice_segments"]),
        "color_palette": scene["color_palette"][:4]
    })

# Also store the full prompts list in a separate file for the pipeline
prompts_list_path = os.path.join(OUTPUT_DIR, "all_video_prompts.json")
with open(prompts_list_path, "w") as f:
    json.dump(all_video_prompts, f, indent=2)

with open(MANIFEST_PATH, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Manifest: {MANIFEST_PATH}")
print(f"All prompts: {prompts_list_path}")

# ============================================================
# FINAL SUMMARY
# ============================================================
print(f"\n{'='*60}")
print(f"PRODUCTION BUILD COMPLETE")
print(f"{'='*60}")
print(f"Scenes: {len(scenes)}")
print(f"Runtime: {total_dur/60:.1f} min")
print(f"Video clips: {len(all_video_prompts)}")
print(f"LTX-2.3 generations: {sum(c['ltx_clips_needed'] for c in all_video_prompts)}")
print(f"Narration segments: {total_voice_segments}")
print(f"Narration words: {total_words} (~{total_words/150:.0f} min)")
print(f"\nAct breakdown:")
for act in manifest["acts"]:
    print(f"  Act {act['act']}: {act['title']:25s} | {act['duration_min']:4.1f}m | {act['clip_count']:3d} clips | {act['narration_words']:5d} words")
print(f"\nOutput files:")
print(f"  {OTIO_PATH}")
print(f"  {MANIFEST_PATH}")
print(f"  {prompts_list_path}")
print(f"  {NARRATION_DIR}/ ({len(os.listdir(NARRATION_DIR))} files)")
print(f"  {PROMPTS_DIR}/ ({len(os.listdir(PROMPTS_DIR))} files)")
