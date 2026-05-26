"""Pipeline v4: Event-sourced, OTIO-derived graph with orchestrator agent.

Architecture:
- Event log (JSONL) is the ONLY source of truth
- OTIO and job queue are read models rebuilt from events
- Orchestrator agent decides which agent runs next
- Agents produce free text; instructor parses into typed Effect
- Bash is the agent's tool; effects record OUTCOMES
- No timeouts anywhere
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from datetime import datetime
from typing import Any

import opentimelineio as otio
from openai import OpenAI

from effects import (
    Effect,
    GenerateNarrationAudio,
    JobCompleted,
    JobFailed,
    JobQuestionAnswered,
    JobQuestionReceived,
    JobRequeued,
    JobStarted,
    NoOp,
    QAFailed,
    QAPassed,
    RenderVideoSegment,
    UpdateScript,
    VMAllocated,
    VMDeallocated,
    VMProvisionFailed,
)
from effect_parser import build_clarification_request, parse_agent_text_multi
from event_store import EventStore
from projection_handler import apply_event
from queue_projection import (
    get_completed_jobs,
    get_pending_jobs,
    get_queue_summary,
    project_queue,
)
from qa_gates_v4 import (
    qa_audio_completeness,
    qa_duration_align,
    qa_stills_judge,
    qa_video_artifact_probe,
)
from skill_loader import get_skill_prompt_fragment, load_skill, read_skill_resource
from search_tools import search_brave, search_perplexity, search_exa


# ---------------------------------------------------------------------------
# DeepSeek client
# ---------------------------------------------------------------------------

_DEEPSEEK_API_KEY = ""
_deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
if os.path.exists(_deepseek_key_path):
    with open(_deepseek_key_path) as _f:
        _DEEPSEEK_API_KEY = _f.read().strip()

_VAST_API_KEY = ""
_vast_key_path = "/Users/orpington/api_keys/LLMS/vast_api_key.txt"
if os.path.exists(_vast_key_path):
    with open(_vast_key_path) as _f:
        _VAST_API_KEY = _f.read().strip()

_DS_CLIENT = OpenAI(api_key=_DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")


# ---------------------------------------------------------------------------
# Agent prompts
# ---------------------------------------------------------------------------

AGENT_PROMPTS: dict[str, str] = {
    "orchestrator": """You are the pipeline orchestrator — a systems architect who maintains global state awareness and diagnoses problems before they cascade.

Your job:
1. Read the current world state (OTIO + queue)
2. Diagnose any anomalies: stuck jobs, failed VMs, missing media, duration mismatches
3. Decide which agent should run next
4. Suggest what that agent should do, including any troubleshooting steps

Respond in this format:

NEXT_AGENT: <agent_id>
REASON: <why this agent, including any diagnosed issues>
PROMPT_HINT: <what to tell the agent, including troubleshooting guidance>

Agents: scenario, audio, video, assembly, provisioner

DECISION RULES (default flow):
- If no script exists → scenario
- If script exists but no audio jobs → audio
- If audio jobs pending/running AND no VM is loading/active → provisioner
- If audio jobs pending/running AND a VM is loading/active → WAIT (do not provision another)
- If audio complete but no video jobs → video
- If video jobs pending/running AND no VM is loading/active → provisioner
- If video jobs pending/running AND a VM is loading/active → WAIT
- If all media complete → assembly
- If output exists → DONE

TROUBLESHOOTING RULES (override defaults when problems detected):
- If a job has failed 3+ times → suggest the agent adjust parameters (shorter text, simpler prompt, different voice)
- If a VM has been idle >30 min but jobs are pending → provisioner should destroy and recreate
- If audio duration differs wildly from video duration → suggest assembly agent loop video or trim audio
- If QA failed with "frozen frames" → suggest video agent increase motion keywords in prompt
- If QA failed with "abrupt audio cut" → suggest audio agent split long text into shorter phrases
- If a worker asked a QUESTION and awaits answer → DO NOT dispatch new work; answer the question first

You are not a traffic cop. You are a diagnostician. State the problem, propose the fix, and choose the right agent to execute it.

{skill_fragment}
""",
    "scenario": """You are a documentary scriptwriter — a storyteller who crafts compelling 30-second narratives.

Your job:
- Write narration text (3 versions: V1 primary, V2 alternate, V3 third take)
- Describe visual shots that match the narration
- Craft a dopamine hook for the opening
- Add pronunciation hints for tricky words
- Estimate duration

IMPORTANT: Write in flowing paragraphs, NOT bulleted lists, NOT JSON, NOT tables. Your response is prose.
But make your prose DENSE with concrete specifics. Every piece of text you write should be the ACTUAL content that will be used — not a description of what would go there, not a placeholder, not a summary.

For example, GOOD prose looks like this:
  V1: "In the ADHD brain, the rainbow isn't magic — it's a lens flare from overstimulated neurons. The reward system is always on, chasing the next bright thing. This is why focus feels impossible, and why the world seems too loud, too fast, too much."
  V2: "For someone with ADHD, a rainbow isn't a gentle arc of color. It's a sensory flood, every wavelength screaming for attention at once. The brain is wired to chase intensity, not balance."
  Visual Notes: A single figure on a windswept hill at golden hour, wide establishing shot, slow dolly in as the narrator speaks, lens flare visible on the right edge, shallow depth of field. Cut to extreme close-up of eyes tracking something unseen. The sky is overcast but a shaft of light breaks through.

BAD prose looks like this:
  V1: "A narration about ADHD and rainbows"
  Visual Notes: "Some scenic shots"

Do NOT write bad prose. The pipeline reads your words directly — abbreviations and vagueness will be used verbatim.

If the script already exists and looks good, say "NoOp: script already exists."

TROUBLESHOOTING & ADAPTATION:
- If the brief is vague or unclear, ASK for clarification rather than guessing.
- If the topic is technical, break jargon into simpler language and add pronunciation guides.
- If a previous script was rejected (check state), diagnose why: too long? off-topic? boring? Fix the specific issue.
- If narration text exceeds ~20 words per scene, warn that TTS may truncate — suggest splitting into multiple scenes.
- If visual notes seem impossible to render, suggest a more feasible alternative.

{skill_fragment}
""",
    "audio": """You are an audio producer for documentaries — an engineer who turns scripts into spoken word that moves people.

Your job:
- Plan narration audio jobs from the script
- Match voices (V1, V2, V3) to scene mood and character
- Provide exact, clean text for TTS synthesis

IMPORTANT: Write in flowing paragraphs, NOT bulleted lists, NOT JSON, NOT tables. Your response is prose.
But make your prose DENSE with concrete specifics. The text you provide is what the TTS engine will speak, word for word.

For each audio job, describe it naturally and include these specifics inline:
- Which voice (V1, V2, or V3)
- The EXACT narration text to synthesize — every single word, punctuation mark, and pause matters
- Which scene it belongs to

For example, GOOD prose looks like this:
  "I'll generate the opening narration in V1. The text is: 'In the ADHD brain, the reward system is always on, chasing the next bright thing. This is why focus feels impossible, and why the world seems too loud, too fast, too much.' That's scene 1, running about 10 seconds. I'll also do V2 with a calmer tone: 'For someone with ADHD, every rainbow is a sensory flood, every wavelength screaming for attention.'"

BAD prose looks like this:
  "I will create audio for the script. Voice: V1. Text: the narration from the script."

Do NOT write bad prose. If you do not include the full verbatim text, the TTS engine will have nothing to speak.

You may plan multiple audio jobs in one response. Describe each one clearly in prose.

If no script exists or jobs already exist, say "NoOp: waiting."

TROUBLESHOOTING & BEST PRACTICES:
- If text is longer than ~25 words, split into multiple shorter clips. Qwen3-TTS handles ~20-25 words cleanly.
- If a previous audio job FAILED with "abrupt cut" or "truncated", the text was too long. Split it.
- If a voice sounds wrong for the mood, suggest a different voice mapping.
- If pronunciation hints exist in the script, pass them through explicitly.
- Strip stage directions like "(sigh)" or "[pause]" — TTS cannot act.
- If audio QA failed with "trailing silence too short", split and retry.

{skill_fragment}
""",
    "video": """You are a video director for documentaries.

Your job:
- Plan video clips from the script's visual notes
- Describe what should appear on screen so the LTX-2.3 video generator can render it
- Specify duration per scene

IMPORTANT: Write in flowing paragraphs, NOT bulleted lists, NOT JSON, NOT tables. Your response is prose.
But make your prose DENSE with concrete specifics. Every description you write becomes the actual prompt fed to the video generator. Vague descriptions produce vague video.

For example, GOOD prose looks like this:
  "Scene 1, 4 seconds: a calm ocean at sunset. Gentle waves roll toward the shore. The sky is warm orange and pink. A few birds fly across the horizon. The water reflects the sunset colors."
  "Scene 2, 5 seconds: a person walks through a forest. Sunlight filters through the trees. Green leaves sway slightly in a breeze. Dappled light moves across the ground."

BAD prose looks like this:
  "I'll create a video for scene 1. It'll be a nice sunset scene with cinematic quality."
  "4k, cinematic, ocean, sunset, best quality, golden hour, shallow depth of field"

Do NOT write bad prose. The video generator receives your description exactly as written. If you say "a nice scene," you will get a generic blur. If you describe gentle waves rolling toward a shore of dark wet sand under a warm orange sky, you will get that exact scene.

How to write reliably: describe what's visible, what's moving, and where it happens. Use present-tense verbs: "waves roll," "leaves sway," "birds fly," "steam rises." One clear motion per clip is enough. The model handles a single action well. Stack three unrelated actions and motion collapses into a still image.

You do NOT need cinematography jargon. Terms like "whip pan," "push-in," "rack focus," or "dolly zoom" are advanced techniques that fail too often to be worth using now. We will add them later once basic generation is reliable. For now, plain language works better.

You do NOT need quality words like "cinematic," "best quality," "4k," or "highly detailed." The model ignores them or gets confused by them. Describe the actual scene instead.

You may plan multiple video jobs in one response. Describe each one clearly in prose.

If no script exists or jobs already exist, say "NoOp: waiting."

TROUBLESHOOTING & BEST PRACTICES:
- If a previous clip came out as a still image with no movement, your motion description was too weak. Add a clear present-tense verb: "waves roll," "clouds drift," "wind moves the grass."
- If a previous clip failed entirely, simplify. Shorter prompts with one subject and one motion are more reliable than complex multi-subject scenes.
- Start with 3-5 second clips. Short clips render faster and fail less often.
- Avoid text, logos, or human faces in descriptions — the generator struggles with those.
- One scene per clip. Don't try to pack a whole movie into one prompt.
- If you want advanced guidance (frame counts, resolution constraints, step counts), load the video-generation skill.

{skill_fragment}
""",
    "assembly": """You are a video editor who assembles documentaries — a craftsman who weaves audio and video into a cohesive whole.

Your job:
- Plan the final mux: which audio clips and video clips go together
- Specify ffmpeg commands for merging
- Handle duration mismatches gracefully

IMPORTANT: Write in flowing paragraphs, NOT bulleted lists, NOT JSON, NOT tables. Your response is prose.
But make your prose DENSE with concrete specifics. When you describe a mux plan, include the exact clip names, durations, and ffmpeg flags.

For example, GOOD prose looks like this:
  "I'm ready to assemble the final cut. I'll combine the completed audio clip (scene1_narration_v1.wav, 8.3 seconds) with the video clip (scene1_visual.mp4, 5 seconds). Since the audio is longer, I'll loop the video with -stream_loop -1 and trim both to the audio duration using -shortest. The ffmpeg command will be: ffmpeg -y -i scene1_visual.mp4 -i scene1_narration_v1.wav -stream_loop -1 -c:v copy -c:a aac -shortest final_scene1.mp4"

BAD prose looks like this:
  "I'll merge the audio and video files."

Do NOT write bad prose. The pipeline needs to know exactly which files, what durations, and what flags — ambiguity will cause silent failures.

If audio/video is not ready or QA-flagged, say "NoOp: waiting for media."

TROUBLESHOOTING & BEST PRACTICES:
- If audio duration > video duration, loop video with `-stream_loop -1` and trim to audio length.
- If video duration > audio duration, trim video to match audio with `-t <audio_dur>`.
- If durations differ by >5x, flag as a problem and suggest re-rendering.
- If QA flagged "frozen frames", do NOT use that clip. Suggest re-rendering.
- If QA flagged "abrupt audio cut", do NOT use that audio. Suggest re-synthesizing.
- If ffmpeg fails with "Invalid data", check files exist and are not 0 bytes.
- If output file is <1KB, it failed silently. Report the failure.

{skill_fragment}
""",
    "provisioner": """You are a DevOps engineer who provisions GPU VMs on Vast.ai.

Your job:
- Provision VMs for audio (TTS) and video (LTX-2.3) workers
- Destroy idle or failed VMs

IMPORTANT: Write in flowing paragraphs, NOT bulleted lists, NOT JSON, NOT tables. Your response is prose.
But make your prose DENSE with concrete specifics. When you decide to provision or destroy a VM, describe the action clearly with the exact numbers and identifiers.

For example, GOOD prose looks like this:
  "I inspected the current state. There are no active instances. I'll provision an audio worker from offer 18923452, which is an RTX 4090 with 24GB VRAM at $0.42/hr. That should handle TTS jobs. For video, I found offer 18924111, an H100 with 80GB at $2.10/hr — that's our LTX-2.3 renderer."

BAD prose looks like this:
  "I will provision some VMs for audio and video work."

Do NOT write bad prose. The pipeline needs the exact offer IDs and GPU types to act. Vagueness will cause provisioning to fail.

You may also use bash for READ-ONLY inspection:
  $ vastai show instances --raw
  $ vastai search offers "gpu_ram >= 24 num_gpus = 1" --raw
The pipeline will execute inspection commands and return results.

NEVER run `vastai create instance` or `vastai destroy instance` via bash — those are actions the pipeline handles.

If no action is needed, say "NoOp: nothing to provision."

TROUBLESHOOTING:
- If search returns nothing, retry with lower requirements.
- Audio VMs: 24GB VRAM (RTX 4090, A5000, A40).
- Video VMs: ≥48GB VRAM (H100, H200, A100 80GB).
- Destroy VMs idle >30 min or running >4 hours without active jobs.

{skill_fragment}
""",
}


# ---------------------------------------------------------------------------
# State projection
# ---------------------------------------------------------------------------

def project_state(event_log_path: str, timeline_path: str) -> tuple[otio.schema.Timeline, dict, dict]:
    """Read event log and project both OTIO and queue state.

    Returns: (timeline, queue_jobs, events_list)
    """
    event_store = EventStore(event_log_path)
    records = event_store.read_all()
    events = [r.effect for r in records]

    # Project OTIO
    if os.path.exists(timeline_path):
        try:
            timeline = otio.schema.Timeline.from_json_file(timeline_path)
        except Exception:
            timeline = otio.schema.Timeline(name="documentary")
            stack = otio.schema.Stack(name="tracks")
            timeline.tracks = stack
            stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
            stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))
    else:
        timeline = otio.schema.Timeline(name="documentary")
        stack = otio.schema.Stack(name="tracks")
        timeline.tracks = stack
        stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
        stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))

    for effect in events:
        timeline = apply_event(timeline, effect)

    # Project queue
    queue_jobs = project_queue(events)

    return timeline, queue_jobs, events


def build_state_summary(timeline: otio.schema.Timeline, queue_jobs: dict, events: list = None) -> str:
    """Build a human-readable state summary for the orchestrator."""
    meta = timeline.metadata.get("documentary", {})
    lines = []

    has_script = bool(meta.get("narration_v1"))
    lines.append(f"Script exists: {has_script}")
    if has_script:
        lines.append(f"  V1: {meta.get('narration_v1', '')[:80]}...")
        lines.append(f"  Duration: {meta.get('duration_sec', 30)}s")

    audio_summary = get_queue_summary(queue_jobs, "audio")
    video_summary = get_queue_summary(queue_jobs, "video")
    lines.append(f"Audio queue: {audio_summary}")
    lines.append(f"Video queue: {video_summary}")

    pending_audio = get_pending_jobs(queue_jobs, "audio")
    pending_video = get_pending_jobs(queue_jobs, "video")
    lines.append(f"Pending audio jobs: {len(pending_audio)}")
    lines.append(f"Pending video jobs: {len(pending_video)}")

    completed_audio = get_completed_jobs(queue_jobs, "audio")
    completed_video = get_completed_jobs(queue_jobs, "video")
    lines.append(f"Completed audio jobs: {len(completed_audio)}")
    lines.append(f"Completed video jobs: {len(completed_video)}")

    # VM status from events
    if events:
        vms: dict[str, dict] = {}
        for e in events:
            if e.effect_type == "VMAllocated":
                vms[e.instance_id] = {
                    "offer_id": e.offer_id,
                    "gpu_type": e.gpu_type,
                    "worker_url": e.worker_url,
                    "status": "allocated",
                }
            elif e.effect_type == "VMDeallocated" and e.instance_id in vms:
                vms[e.instance_id]["status"] = "destroyed"
            elif e.effect_type == "VMProvisionFailed" and e.offer_id:
                # Find by offer_id
                for vid, vm in vms.items():
                    if vm.get("offer_id") == e.offer_id:
                        vm["status"] = "failed"
        active = [vm for vm in vms.values() if vm["status"] == "allocated"]
        if active:
            lines.append(f"Active VMs: {len(active)}")
            for vm in active:
                url = vm.get("worker_url", "(no url yet)")
                lines.append(f"  {vm['gpu_type']} → {url}")
        else:
            lines.append("Active VMs: 0")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent calling
# ---------------------------------------------------------------------------

def _inject_skill_fragment(agent_id: str, prompt: str) -> str:
    """Replace {skill_fragment} placeholder with actual skill discovery text."""
    fragment = get_skill_prompt_fragment(agent_id)
    return prompt.replace("{skill_fragment}", fragment)


def _handle_agent_research(response: str) -> str | None:
    """Detect RESEARCH markers and execute search. Returns result or None."""
    lines = response.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("RESEARCH_DEEP:"):
            query = stripped.split(":", 1)[1].strip()
            return f"[RESEARCH_DEEP result for '{query}']\n{search_perplexity(query, count=3)}"
        if stripped.upper().startswith("RESEARCH_NEWS:"):
            query = stripped.split(":", 1)[1].strip()
            return f"[RESEARCH_NEWS result for '{query}']\n{search_exa(query, count=3)}"
        if stripped.upper().startswith("RESEARCH:"):
            query = stripped.split(":", 1)[1].strip()
            return f"[RESEARCH result for '{query}']\n{search_brave(query, count=3)}"
    return None


def _handle_agent_skill_load(response: str) -> str | None:
    """Detect LOAD_SKILL or READ_RESOURCE markers and load content. Returns result or None."""
    lines = response.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("LOAD_SKILL:"):
            name = stripped.split(":", 1)[1].strip()
            return f"[SKILL LOADED: {name}]\n{load_skill(name)}"
        if stripped.upper().startswith("READ_RESOURCE:"):
            path = stripped.split(":", 1)[1].strip()
            if "/" in path:
                skill_name, resource = path.split("/", 1)
                return f"[RESOURCE: {path}]\n{read_skill_resource(skill_name, resource)}"
            return f"[ERROR] READ_RESOURCE format is '<skill_name>/<path>'. Got: {path}"
    return None


def _handle_agent_bash(response: str) -> str | None:
    """Detect bash commands embedded in agent responses (for provisioner). Returns result or None.

    Looks for lines starting with '$ ' or fenced bash blocks and executes them.
    """
    import re
    commands: list[str] = []
    in_bash = False
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("```bash") or stripped.startswith("```sh"):
            in_bash = True
            continue
        if stripped == "```" and in_bash:
            in_bash = False
            continue
        if in_bash:
            commands.append(stripped)
        elif stripped.startswith("$ "):
            commands.append(stripped[2:])
    if not commands:
        return None
    results: list[str] = []
    for cmd in commands:
        if not cmd.strip():
            continue
        result = run_bash(cmd)
        results.append(f"$ {cmd}\nexit={result['returncode']}\nstdout:\n{result['stdout'][:2000]}\nstderr:\n{result['stderr'][:1000]}")
    return "[BASH RESULTS]\n" + "\n---\n".join(results)


async def call_agent(agent_id: str, prompt: str) -> str:
    """Call an agent via direct LLM API with multi-turn skill/research support.

    The agent may request:
    - LOAD_SKILL: <name> → full skill instructions injected
    - READ_RESOURCE: <skill>/<path> → specific resource file
    - RESEARCH: <query> → Brave search
    - RESEARCH_DEEP: <query> → Perplexity synthesis
    - RESEARCH_NEWS: <query> → Exa recent results
    - Bash commands (for provisioner inspection only)

    Loops until the agent returns a final response (no tool markers).
    No artificial turn limit — agents can take as many turns as needed.
    """
    system_prompt = _inject_skill_fragment(agent_id, AGENT_PROMPTS.get(agent_id, ""))
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    turn = 0
    while True:
        turn += 1
        result = _DS_CLIENT.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.7,
        )
        response = str(result.choices[0].message.content)

        # Check for skill load requests
        skill_result = _handle_agent_skill_load(response)
        if skill_result:
            print(f"  [SKILL TURN {turn}] Agent requested skill/research")
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {"role": "user", "content": f"You requested additional information. Here is the result:\n\n{skill_result}\n\nNow continue with your task."}
            )
            continue

        # Check for research requests
        research_result = _handle_agent_research(response)
        if research_result:
            print(f"  [RESEARCH TURN {turn}] Agent requested research")
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {"role": "user", "content": f"You requested research. Here are the results:\n\n{research_result}\n\nNow continue with your task."}
            )
            continue

        # Check for embedded bash (provisioner inspects Vast.ai)
        bash_result = _handle_agent_bash(response)
        if bash_result:
            print(f"  [BASH TURN {turn}] Agent requested bash execution")
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {"role": "user", "content": f"You requested bash commands. Here are the results:\n\n{bash_result}\n\nNow continue with your task."}
            )
            continue

        # No markers — return final response
        return response


# ---------------------------------------------------------------------------
# Bash execution (not an effect — agents use bash freely)
# ---------------------------------------------------------------------------

def run_bash(command: str) -> dict[str, Any]:
    """Execute a bash command. Returns result dict."""
    print(f"  [BASH] {command[:100]}...")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": -1}


# ---------------------------------------------------------------------------
# VM operations (via bash, recorded as effects)
# ---------------------------------------------------------------------------

def destroy_orphan_vms(event_store: EventStore) -> None:
    """Find and destroy orphan VMs with documentary-* labels, recording VMDeallocated effects."""
    print("[CLEANUP] Checking for orphan VMs...")
    result = run_bash("vastai show instances --raw 2>/dev/null || echo '[]'")
    if result["returncode"] != 0:
        print(f"  [CLEANUP] vastai failed: {result['stderr'][:200]}")
        return

    try:
        instances = json.loads(result["stdout"])
        if not isinstance(instances, list):
            instances = []
    except json.JSONDecodeError:
        instances = []

    destroyed = 0
    for inst in instances:
        inst_id = inst.get("id")
        label = inst.get("label", "")
        if inst_id and label and label.startswith("documentary-"):
            run_bash(f"vastai destroy instance {inst_id}")
            event_store.append(
                VMDeallocated(
                    agent_id="pipeline",
                    instance_id=str(inst_id),
                    reason="orphan cleanup at startup",
                ),
                otio_hash_before="",
            )
            destroyed += 1

    print(f"[CLEANUP] Destroyed {destroyed} orphan VM(s)")


# ---------------------------------------------------------------------------
# Worker dispatch — REAL workers only
# ---------------------------------------------------------------------------

async def _post_to_vm(client, worker_url: str, text: str) -> str:
    """POST text to a VM agent and return the response text."""
    resp = await client.post(
        f"{worker_url.rstrip('/')}/",
        content=text.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    resp.raise_for_status()
    return resp.text


def _parse_vm_response(response: str) -> tuple[str, str]:
    """Parse a VM agent response for markers.

    Returns (response_type, content) where response_type is one of:
    "result", "question", "error", or "unknown".
    """
    stripped = response.strip()
    if stripped.upper().startswith("RESULT:"):
        return "result", stripped[7:].strip()
    if stripped.upper().startswith("QUESTION:"):
        return "question", stripped[9:].strip()
    if stripped.upper().startswith("ERROR:"):
        return "error", stripped[6:].strip()
    # Fallback: if response contains a workspace path, treat as result
    if "/workspace/output/" in stripped:
        return "result", stripped
    return "unknown", stripped


def _extract_artifact_path(response_text: str, job_id: str, stage: str) -> str:
    """Try to find an artifact path in the VM response."""
    import re
    m = re.search(r"/workspace/output/[^\s\n]+\.(wav|mp4)", response_text)
    if m:
        return m.group(0)
    return f"/workspace/output/{job_id}.{'wav' if stage == 'audio' else 'mp4'}"


def _download_artifact(instance_id: str, remote_path: str, output_dir: str) -> str:
    """Download an artifact from a VM. Returns local path or empty string on failure."""
    if not instance_id or not remote_path:
        return ""
    artifact_name = os.path.basename(remote_path)
    local_dir = os.path.join(output_dir, "artifacts")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, artifact_name)
    if os.path.exists(local_path):
        return local_path
    download_cmd = f"vastai copy {instance_id}:{remote_path} {local_path}"
    result = run_bash(download_cmd)
    if result["returncode"] == 0 and os.path.exists(local_path):
        return local_path
    print(f"  [DOWNLOAD] Failed: {result['stderr'][:200]}")
    return ""


async def dispatch_pending_jobs(
    queue_jobs: dict,
    event_store: EventStore,
    output_dir: str,
    script_meta: dict,
) -> bool:
    """Dispatch jobs to real workers via HTTP, handling collaboration turns.

    Processes three kinds of jobs:
    - pending / needs_retry: send initial instruction
    - running with question_answer: send the answer to a VM's question

    Returns True if any work was dispatched.
    """
    import httpx

    acted = False

    # Discover worker URLs and instance IDs from VM effects
    records = event_store.read_all()
    worker_urls: dict[str, str] = {}  # stage -> worker_url
    worker_instances: dict[str, str] = {}  # worker_url -> instance_id
    for r in records:
        if r.effect.effect_type == "VMAllocated":
            eff = r.effect
            if hasattr(eff, "worker_url") and eff.worker_url:
                worker_instances[eff.worker_url] = getattr(eff, "instance_id", "")
                if "tts" in (eff.gpu_type or "").lower() or "audio" in str(getattr(eff, "label", "")).lower():
                    worker_urls["audio"] = eff.worker_url
                else:
                    worker_urls["video"] = eff.worker_url

    # Build script context once
    script_context = ""
    if script_meta.get("has_script"):
        script_context = (
            f"SCRIPT CONTEXT:\n"
            f"Scene: {script_meta.get('scene_num', 1)}\n"
            f"Duration: {script_meta.get('duration_sec', 30)}s\n"
            f"Dopamine hook: {script_meta.get('dopamine_hook', '')[:100]}...\n"
            f"Visual notes: {script_meta.get('visual_notes', '')[:100]}...\n"
            f"Pronunciation: {script_meta.get('pronunciation_hints', '')[:100]}...\n\n"
        )

    async with httpx.AsyncClient() as client:
        for job in queue_jobs.values():
            worker_url = worker_urls.get(job.stage)
            if not worker_url:
                continue

            instance_id = worker_instances.get(worker_url, "")

            # -----------------------------------------------------------------
            # Case 1: job has a pending answer — send it to the VM
            # -----------------------------------------------------------------
            if job.status == "running" and getattr(job, "question_answer", ""):
                answer_text = getattr(job, "question_answer", "")
                print(f"  [WORKER] Sending answer to {job.job_id} at {worker_url}")
                try:
                    agent_response = await _post_to_vm(client, worker_url, answer_text)
                    print(f"  [WORKER] {job.job_id} response: {agent_response[:200]}...")
                    resp_type, resp_content = _parse_vm_response(agent_response)

                    if resp_type == "result":
                        artifact_path = _extract_artifact_path(agent_response, job.job_id, job.stage)
                        local_path = _download_artifact(instance_id, artifact_path, output_dir)
                        event_store.append(
                            JobCompleted(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                artifact_path=artifact_path,
                                local_artifact_path=local_path,
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} completed → {artifact_path}")
                        acted = True
                    elif resp_type == "question":
                        event_store.append(
                            JobQuestionReceived(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                question=resp_content,
                                worker_url=worker_url,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} asked another question")
                        acted = True
                    elif resp_type == "error":
                        event_store.append(
                            JobFailed(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                error_message=resp_content,
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} failed: {resp_content[:200]}")
                        acted = True
                    else:
                        # Unknown response — treat as error but allow retry
                        event_store.append(
                            JobFailed(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                error_message=f"Unparseable VM response: {agent_response[:300]}",
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        acted = True
                except Exception as exc:
                    print(f"  [WORKER] {job.job_id} failed: {exc}")
                    event_store.append(
                        JobFailed(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            error_message=str(exc)[:500],
                            stage=job.stage,
                        ),
                        otio_hash_before="",
                    )
                    acted = True
                continue

            # -----------------------------------------------------------------
            # Case 2: job is waiting for an answer — skip until answered
            # -----------------------------------------------------------------
            if job.status == "running" and getattr(job, "pending_question", ""):
                print(f"  [WORKER] {job.job_id} awaiting answer — skipping")
                continue

            # -----------------------------------------------------------------
            # Case 3: pending / needs_retry — send initial instruction
            # -----------------------------------------------------------------
            if job.status not in ("pending", "needs_retry"):
                continue

            print(f"  [WORKER] Dispatching {job.job_id} to {worker_url}")

            # Mark started
            event_store.append(
                JobStarted(
                    agent_id="pipeline",
                    job_id=job.job_id,
                    worker_id=worker_url,
                    instance_id=instance_id,
                    stage=job.stage,
                ),
                otio_hash_before="",
            )

            try:
                if job.stage == "audio":
                    instruction = (
                        f"We are producing a documentary scene.\n\n"
                        f"{script_context}"
                        f"Please generate narration audio for this scene.\n"
                        f"The narration text is:\n"
                        f"\"{job.payload.get('text', '')}\"\n\n"
                        f"Use the Qwen3-TTS model (runner at repo/scripts/run_qwen3_tts.py). "
                        f"Save the output to /workspace/output/{job.job_id}.wav.\n\n"
                        f"If anything is unclear, ask. If generation fails, troubleshoot. "
                        f"Report RESULT: with the file path when done."
                    )
                else:
                    instruction = (
                        f"We are producing a documentary scene.\n\n"
                        f"{script_context}"
                        f"Please generate a video clip for this scene.\n"
                        f"The visual description is:\n"
                        f"\"{job.payload.get('prompt', '')}\"\n\n"
                        f"Target duration: {job.payload.get('duration_sec', 5)} seconds.\n"
                        f"Use the LTX-2.3 model (runner at repo/scripts/run_ltx_2_3.py). "
                        f"Save the output to /workspace/output/{job.job_id}.mp4.\n\n"
                        f"If anything is unclear, ask. If generation fails, troubleshoot. "
                        f"Report RESULT: with the file path when done."
                    )

                agent_response = await _post_to_vm(client, worker_url, instruction)
                print(f"  [WORKER] {job.job_id} response: {agent_response[:200]}...")
                resp_type, resp_content = _parse_vm_response(agent_response)

                if resp_type == "result":
                    artifact_path = _extract_artifact_path(agent_response, job.job_id, job.stage)
                    local_path = _download_artifact(instance_id, artifact_path, output_dir)
                    event_store.append(
                        JobCompleted(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            artifact_path=artifact_path,
                            local_artifact_path=local_path,
                            stage=job.stage,
                        ),
                        otio_hash_before="",
                    )
                    print(f"  [WORKER] {job.job_id} completed → {artifact_path}")
                    acted = True
                elif resp_type == "question":
                    event_store.append(
                        JobQuestionReceived(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            question=resp_content,
                            worker_url=worker_url,
                        ),
                        otio_hash_before="",
                    )
                    print(f"  [WORKER] {job.job_id} asked: {resp_content[:200]}")
                    acted = True
                elif resp_type == "error":
                    event_store.append(
                        JobFailed(
                            agent_id="pipeline",
                            job_id=job.job_id,
                            error_message=resp_content,
                            stage=job.stage,
                        ),
                        otio_hash_before="",
                    )
                    print(f"  [WORKER] {job.job_id} failed: {resp_content[:200]}")
                    acted = True
                else:
                    # Unknown response — try to extract artifact path anyway
                    artifact_path = _extract_artifact_path(agent_response, job.job_id, job.stage)
                    local_path = _download_artifact(instance_id, artifact_path, output_dir)
                    if local_path:
                        event_store.append(
                            JobCompleted(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                artifact_path=artifact_path,
                                local_artifact_path=local_path,
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        print(f"  [WORKER] {job.job_id} completed → {artifact_path}")
                        acted = True
                    else:
                        event_store.append(
                            JobFailed(
                                agent_id="pipeline",
                                job_id=job.job_id,
                                error_message=f"Unparseable VM response: {agent_response[:300]}",
                                stage=job.stage,
                            ),
                            otio_hash_before="",
                        )
                        acted = True

            except Exception as exc:
                print(f"  [WORKER] {job.job_id} failed: {exc}")
                event_store.append(
                    JobFailed(
                        agent_id="pipeline",
                        job_id=job.job_id,
                        error_message=str(exc)[:500],
                        stage=job.stage,
                    ),
                    otio_hash_before="",
                )
                acted = True

    return acted


# ---------------------------------------------------------------------------
# Assembly — REAL ffmpeg
# ---------------------------------------------------------------------------

def _ensure_local_artifact(job, output_dir: str) -> str | None:
    """Return a local path for a job's artifact, downloading from VM if needed."""
    # Prefer already-downloaded local path
    local = getattr(job, "local_artifact_path", "")
    if local and os.path.exists(local):
        return local

    # Try to download from VM
    instance_id = getattr(job, "instance_id", "")
    remote = job.artifact_path
    if not instance_id or not remote:
        print(f"  [ASSEMBLY] No VM info for {job.job_id}")
        return None

    artifact_name = os.path.basename(remote)
    local_dir = os.path.join(output_dir, "artifacts")
    os.makedirs(local_dir, exist_ok=True)
    local = os.path.join(local_dir, artifact_name)

    if os.path.exists(local):
        return local

    download_cmd = f"vastai copy {instance_id}:{remote} {local}"
    result = run_bash(download_cmd)
    if result["returncode"] == 0 and os.path.exists(local):
        print(f"  [ASSEMBLY] Downloaded {job.job_id} → {local}")
        return local
    else:
        print(f"  [ASSEMBLY] Download failed for {job.job_id}: {result['stderr'][:200]}")
        return None


def run_assembly(output_dir: str, queue_jobs: dict) -> bool:
    """Assemble final video from completed audio/video clips using ffmpeg.

    Returns True if assembly was attempted.
    """
    output_path = os.path.join(output_dir, "output", "documentary.mp4")
    if os.path.exists(output_path):
        return False

    completed_audio = get_completed_jobs(queue_jobs, "audio")
    completed_video = get_completed_jobs(queue_jobs, "video")

    if not completed_audio or not completed_video:
        return False

    os.makedirs(os.path.join(output_dir, "output"), exist_ok=True)

    audio_path = _ensure_local_artifact(completed_audio[0], output_dir)
    video_path = _ensure_local_artifact(completed_video[0], output_dir)

    if not audio_path:
        print(f"  [ASSEMBLY] Audio not available locally: {completed_audio[0].job_id}")
        return False
    if not video_path:
        print(f"  [ASSEMBLY] Video not available locally: {completed_video[0].job_id}")
        return False

    cmd = (
        f"ffmpeg -y -i {video_path} -i {audio_path} "
        f"-c:v copy -c:a aac -shortest {output_path}"
    )
    result = run_bash(cmd)

    if result["returncode"] == 0 and os.path.exists(output_path):
        print(f"  [ASSEMBLY] Created {output_path}")
        return True
    else:
        print(f"  [ASSEMBLY] ffmpeg failed: {result['stderr'][:200]}")
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def consult_orchestrator(state_summary: str, brief: str) -> dict[str, str]:
    """Ask the orchestrator agent what to do next.

    Returns: {"next_agent": str, "reason": str, "prompt_hint": str}
    """
    prompt = f"""Current state:
{state_summary}

Brief: {brief}

What should the pipeline do next?"""

    response = await call_agent("orchestrator", prompt)
    print(f"  [ORCHESTRATOR] {response[:200]}...")

    # Parse the orchestrator response
    next_agent = ""
    reason = ""
    prompt_hint = ""

    for line in response.splitlines():
        if line.startswith("NEXT_AGENT:"):
            next_agent = line.split(":", 1)[1].strip().lower()
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif line.startswith("PROMPT_HINT:"):
            prompt_hint = line.split(":", 1)[1].strip()

    # Fallback: if parsing failed, use simple heuristic
    if not next_agent:
        if "scenario" in response.lower():
            next_agent = "scenario"
        elif "audio" in response.lower():
            next_agent = "audio"
        elif "video" in response.lower():
            next_agent = "video"
        elif "assembly" in response.lower():
            next_agent = "assembly"
        elif "provisioner" in response.lower():
            next_agent = "provisioner"
        elif "done" in response.lower():
            next_agent = "done"

    return {
        "next_agent": next_agent,
        "reason": reason,
        "prompt_hint": prompt_hint,
    }


async def answer_pending_questions(queue_jobs: dict, event_store: EventStore, brief: str) -> bool:
    """Find jobs where the worker asked a question and generate answers.

    Uses the orchestrator to decide how to answer each question.
    Appends JobQuestionAnswered events.
    Returns True if any answers were produced.
    """
    acted = False
    for job in queue_jobs.values():
        question = getattr(job, "pending_question", "")
        if not question or job.status != "running":
            continue

        print(f"  [COLLAB] {job.job_id} question: {question[:200]}...")

        # Build a collaboration prompt for the orchestrator
        collab_prompt = (
            f"A worker agent asked a question about a job it is processing.\n\n"
            f"Job: {job.job_id} (stage={job.stage})\n"
            f"Question from worker: {question}\n\n"
            f"Documentary brief: {brief}\n\n"
            f"Please provide a concise, actionable answer for the worker. "
            f"The worker is a smart agent that can troubleshoot — give it the "
            f"information it needs, not a script to run."
        )

        try:
            response = await call_agent("orchestrator", collab_prompt)
            answer = response.strip()
            if answer:
                event_store.append(
                    JobQuestionAnswered(
                        agent_id="orchestrator",
                        job_id=job.job_id,
                        answer=answer,
                    ),
                    otio_hash_before="",
                )
                print(f"  [COLLAB] {job.job_id} answer: {answer[:200]}...")
                acted = True
        except Exception as exc:
            print(f"  [COLLAB] {job.job_id} failed to get answer: {exc}")

    return acted


def run_qa_gates(queue_jobs: dict, event_store: EventStore) -> bool:
    """Run deterministic QA gates on completed artifacts.

    For each completed job without a corresponding QAPassed/QAFailed event
    that happened AFTER the most recent JobCompleted, run the appropriate gate.
    Returns True if any QA was run.
    """
    acted = False
    records = list(event_store.read_all())

    # Find the latest JobCompleted index for each job
    latest_completion_idx: dict[str, int] = {}
    for i, r in enumerate(records):
        if r.effect.effect_type == "JobCompleted" and r.effect.job_id:
            latest_completion_idx[r.effect.job_id] = i

    # Find QA events that happened after the latest completion
    qa_done: set[str] = set()
    for i, r in enumerate(records):
        if r.effect.effect_type in ("QAPassed", "QAFailed"):
            job_id = r.effect.job_id
            if job_id in latest_completion_idx and i > latest_completion_idx[job_id]:
                qa_done.add(job_id)

    for job in queue_jobs.values():
        if job.status != "completed":
            continue
        if job.job_id in qa_done:
            continue

        local_path = getattr(job, "local_artifact_path", "") or job.artifact_path
        if not local_path or not os.path.exists(local_path):
            print(f"  [QA] {job.job_id} skipped — no local artifact")
            continue

        print(f"  [QA] Running gates for {job.job_id}")

        if job.stage == "audio":
            result = qa_audio_completeness(
                scene_id=job.job_id,
                audio_path=local_path,
            )
            if result["verdict"] == "pass":
                event_store.append(
                    QAPassed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict=result["reason"] if "reason" in result else "audio complete",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} passed audio completeness")
            else:
                event_store.append(
                    QAFailed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict=result.get("reason", "audio QA failed"),
                        comments=[result.get("reason", "")],
                        suggested_fix="Retry audio generation with same or adjusted text",
                    ),
                    otio_hash_before="",
                )
                event_store.append(
                    JobRequeued(
                        agent_id="qa",
                        job_id=job.job_id,
                        comments=[result.get("reason", "")],
                        suggested_fix="Retry audio generation",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} FAILED audio: {result.get('reason', '')}")
            acted = True

        elif job.stage == "video":
            # Run video probe + stills judge
            probe = qa_video_artifact_probe(
                scene_id=job.job_id,
                video_path=local_path,
            )
            stills = qa_stills_judge(
                scene_id=job.job_id,
                video_path=local_path,
            )

            issues: list[str] = []
            if probe["verdict"] == "fail":
                issues.append(probe.get("error", "probe failed"))
            if stills["verdict"] == "fail":
                issues.append(stills.get("reason", "stills judge failed"))

            if not issues:
                event_store.append(
                    QAPassed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict="video probe and motion check passed",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} passed video gates")
            else:
                event_store.append(
                    QAFailed(
                        agent_id="qa",
                        job_id=job.job_id,
                        artifact_path=local_path,
                        verdict="; ".join(issues),
                        comments=issues,
                        suggested_fix="Retry video generation with adjusted prompt or duration",
                    ),
                    otio_hash_before="",
                )
                event_store.append(
                    JobRequeued(
                        agent_id="qa",
                        job_id=job.job_id,
                        comments=issues,
                        suggested_fix="Retry video generation",
                    ),
                    otio_hash_before="",
                )
                print(f"  [QA] {job.job_id} FAILED video: {'; '.join(issues)}")
            acted = True

    return acted


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _build_parse_feedback(agent_id: str, effects: list[Effect], valid_effects: list[Effect]) -> str:
    """Build feedback telling the agent what the parser understood."""
    if valid_effects:
        lines = ["FEEDBACK — Parser understood your response:"]
        for e in valid_effects:
            if e.effect_type == "UpdateScript":
                lines.append(f"  → UpdateScript: V1({len(e.narration_v1)} chars), V2({len(e.narration_v2)} chars), V3({len(e.narration_v3)} chars)")
            elif e.effect_type == "GenerateNarrationAudio":
                lines.append(f"  → GenerateNarrationAudio: voice={e.voice}, text='{e.text[:50]}...'")
            elif e.effect_type == "RenderVideoSegment":
                lines.append(f"  → RenderVideoSegment: duration={e.duration_sec}s, prompt='{e.prompt[:50]}...'")
            elif e.effect_type == "VMAllocated":
                lines.append(f"  → VMAllocated: offer_id={e.offer_id}, gpu_type={e.gpu_type}")
            elif e.effect_type == "VMDeallocated":
                lines.append(f"  → VMDeallocated: instance_id={e.instance_id}, reason={e.reason}")
            elif e.effect_type == "NoOp":
                lines.append(f"  → NoOp: {getattr(e, 'reason', 'no action')}")
            else:
                lines.append(f"  → {e.effect_type}")
        return "\n".join(lines)
    elif effects and all(e.effect_type == "NoOp" for e in effects):
        return (
            "FEEDBACK — Parser found NoOp. If you intended to create an effect, "
            "please describe the concrete details clearly in your prose:\n"
            "  - For audio: specify the voice (V1/V2/V3) and the exact text to synthesize\n"
            "  - For video: describe the visual scene with specific subjects, lighting, motion\n"
            "  - For provisioning: mention the offer ID and GPU type explicitly\n"
            "  - For scripts: include the full narration text for each version"
        )
    else:
        return (
            "FEEDBACK — Parser could not extract any effects from your response. "
            "Please write naturally, but make sure to include the specific concrete details "
            "(exact text, voice names, offer IDs, visual descriptions, etc.) so the pipeline can act on them."
        )


def _build_enactment_feedback(effect: Effect, success: bool, detail: str = "") -> str:
    """Build feedback about what happened when an effect was enacted."""
    if success:
        if effect.effect_type == "VMAllocated":
            return f"ENACTED: VM provisioned. instance_id={effect.instance_id}, worker_url={effect.worker_url}"
        elif effect.effect_type == "VMDeallocated":
            return f"ENACTED: VM destroyed. instance_id={effect.instance_id}"
        elif effect.effect_type == "GenerateNarrationAudio":
            return f"ENACTED: Audio job queued. voice={effect.voice}, text_len={len(effect.text)}"
        elif effect.effect_type == "RenderVideoSegment":
            return f"ENACTED: Video job queued. duration={effect.duration_sec}s"
        elif effect.effect_type == "UpdateScript":
            return f"ENACTED: Script updated. V1={len(effect.narration_v1)} chars"
        else:
            return f"ENACTED: {effect.effect_type} stored successfully"
    else:
        return f"FAILED: {effect.effect_type} could not be enacted. {detail}"


async def run_pipeline(
    brief: str,
    output_dir: str,
    max_cycles: int = 50,
) -> str:
    """Run the pipeline."""
    os.makedirs(output_dir, exist_ok=True)
    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")
    event_log_path = os.path.join(output_dir, "events.jsonl")

    event_store = EventStore(event_log_path)

    # Startup cleanup
    destroy_orphan_vms(event_store)

    print(f"[PIPELINE] Starting: {brief[:60]}")

    # Feedback log: messages from previous cycle(s) for the next agent
    feedback_history: list[str] = []

    for cycle in range(max_cycles):
        print(f"\n[CYCLE {cycle + 1}]")

        # Project state from events
        timeline, queue_jobs, events = project_state(event_log_path, timeline_path)
        state_summary = build_state_summary(timeline, queue_jobs, events)
        print(f"  State summary:\n    {state_summary.replace(chr(10), chr(10) + '    ')}")

        # Check completion
        audio_summary = get_queue_summary(queue_jobs, "audio")
        video_summary = get_queue_summary(queue_jobs, "video")
        output_path = os.path.join(output_dir, "output", "documentary.mp4")

        audio_complete = (
            audio_summary.get("completed", 0) > 0
            and audio_summary.get("pending", 0) == 0
            and audio_summary.get("running", 0) == 0
            and audio_summary.get("needs_retry", 0) == 0
        )
        video_complete = (
            video_summary.get("completed", 0) > 0
            and video_summary.get("pending", 0) == 0
            and video_summary.get("running", 0) == 0
            and video_summary.get("needs_retry", 0) == 0
        )
        has_output = os.path.exists(output_path)

        if audio_complete and video_complete and has_output:
            print("[PIPELINE] Complete!")
            return f"Pipeline complete in {cycle + 1} cycles."

        # Consult orchestrator
        orch = await consult_orchestrator(state_summary, brief)
        next_agent = orch["next_agent"]

        if next_agent == "done" or not next_agent:
            print("[PIPELINE] Orchestrator says done. Stopping.")
            return f"Pipeline stopped after {cycle + 1} cycles."

        print(f"  Orchestrator: run {next_agent} ({orch['reason']})")

        # Build agent prompt
        agent_prompt = AGENT_PROMPTS.get(next_agent, "")
        if orch["prompt_hint"]:
            agent_prompt += f"\n\nHint: {orch['prompt_hint']}"
        agent_prompt += f"\n\n{state_summary}\n\nBrief: {brief}"

        # Append feedback from previous cycles
        if feedback_history:
            agent_prompt += "\n\n--- PREVIOUS CYCLE FEEDBACK ---\n"
            for fb in feedback_history[-3:]:
                agent_prompt += fb + "\n"
            agent_prompt += "--- END FEEDBACK ---\n"
        # Run agent
        cycle_feedback: list[str] = []
        try:
            response = await call_agent(next_agent, agent_prompt)
            print(f"  Response: {response[:200]}...")

            effects = parse_agent_text_multi(next_agent, response)
            print(f"  Raw effects: {[e.effect_type for e in effects]}")

            # Post-parse validation: strict models already validated via instructor.
            # We only filter here for genuinely empty content that slipped through.
            valid_effects = []
            for effect in effects:
                if effect.effect_type == "UpdateScript" and not effect.narration_v1:
                    print(f"  Validation: UpdateScript missing narration_v1 — skipping")
                    continue
                if effect.effect_type == "GenerateNarrationAudio" and not effect.text:
                    print(f"  Validation: GenerateNarrationAudio missing text — skipping")
                    continue
                if effect.effect_type == "RenderVideoSegment" and not effect.prompt:
                    print(f"  Validation: RenderVideoSegment missing prompt — skipping")
                    continue
                valid_effects.append(effect)

            # Post-parse clarification: if nothing valid was extracted, ask the agent directly
            clarification_turns = 0
            while not valid_effects and clarification_turns < 2:
                clarification_turns += 1
                clarification = build_clarification_request(effects)
                if not clarification:
                    break
                print(f"  [CLARIFICATION TURN {clarification_turns}] Asking agent for missing details")
                try:
                    clarification_response = await call_agent(
                        next_agent,
                        f"{agent_prompt}\n\n--- CLARIFICATION REQUEST ---\n{clarification}\n--- END CLARIFICATION ---"
                    )
                    print(f"  Clarification response: {clarification_response[:200]}...")
                    effects = parse_agent_text_multi(next_agent, clarification_response)
                    valid_effects = []
                    for effect in effects:
                        if effect.effect_type == "UpdateScript" and not effect.narration_v1:
                            continue
                        if effect.effect_type == "GenerateNarrationAudio" and not effect.text:
                            continue
                        if effect.effect_type == "RenderVideoSegment" and not effect.prompt:
                            continue
                        valid_effects.append(effect)
                    if valid_effects:
                        print(f"  Clarification succeeded: {[e.effect_type for e in valid_effects]}")
                        break
                except Exception as exc:
                    print(f"  Clarification failed: {exc}")
                    break

            # Parse feedback — tell agent what we understood
            parse_fb = _build_parse_feedback(next_agent, effects, valid_effects)
            print(f"  {parse_fb.splitlines()[0]}")
            cycle_feedback.append(parse_fb)

            # Store effects in event log
            for effect in valid_effects:
                # For VM effects, execute bash first, then record outcome
                if effect.effect_type == "VMAllocated":
                    # Determine mode from pending jobs if gpu_type doesn't specify it
                    mode = "ltx"
                    if "tts" in (effect.gpu_type or "").lower() or "audio" in (effect.gpu_type or "").lower():
                        mode = "tts"
                    elif pending_audio and not pending_video:
                        mode = "tts"
                    elif not pending_audio and pending_video:
                        mode = "ltx"
                    onstart = (
                        "cd /workspace && "
                        "apt-get update -qq && apt-get install -y -qq git curl wget ffmpeg && "
                        "git clone --depth 1 --branch strands-migration https://github.com/OrpingtonClose/economy-documentary.git repo && "
                        f"bash repo/scripts/vm_onstart_{mode}.sh '{_DEEPSEEK_API_KEY}' '{_VAST_API_KEY}'"
                    )
                    cmd = (
                        f"vastai create instance {effect.offer_id} "
                        f"--image nvidia/cuda:12.6.0-cudnn9-runtime-ubuntu22.04 "
                        f"--disk 150 --ssh --direct --label documentary-{mode} "
                        f"--onstart-cmd '{onstart}'"
                    )
                    bash_result = run_bash(cmd)
                    if bash_result["returncode"] == 0:
                        import re
                        m = re.search(r"new_contract['\"]?\s*:\s*(\d+)", bash_result["stdout"])
                        if m:
                            effect.instance_id = m.group(1)
                        # Get worker URL from instance status
                        if effect.instance_id:
                            status_result = run_bash(f"vastai show instance {effect.instance_id} --raw")
                            if status_result["returncode"] == 0:
                                try:
                                    import json
                                    inst_info = json.loads(status_result["stdout"])
                                    public_ip = inst_info.get("public_ipaddr", "")
                                    ports = inst_info.get("ports", {})
                                    port_8880 = ports.get("8880/tcp", [])
                                    if public_ip and port_8880:
                                        host_port = port_8880[0].get("HostPort", "")
                                        if host_port:
                                            effect.worker_url = f"http://{public_ip}:{host_port}/"
                                except Exception:
                                    pass
                        event_store.append(effect, otio_hash_before="")
                        cycle_feedback.append(_build_enactment_feedback(effect, True))
                    else:
                        fail_detail = bash_result["stderr"][:300]
                        event_store.append(
                            VMProvisionFailed(
                                agent_id=next_agent,
                                offer_id=effect.offer_id,
                                error_message=bash_result["stderr"][:500],
                            ),
                            otio_hash_before="",
                        )
                        cycle_feedback.append(_build_enactment_feedback(effect, False, fail_detail))

                elif effect.effect_type == "VMDeallocated":
                    run_bash(f"vastai destroy instance {effect.instance_id}")
                    event_store.append(effect, otio_hash_before="")
                    cycle_feedback.append(_build_enactment_feedback(effect, True))

                elif effect.effect_type != "NoOp":
                    event_store.append(effect, otio_hash_before="")
                    cycle_feedback.append(_build_enactment_feedback(effect, True))

        except Exception as exc:
            print(f"  Error: {exc}")
            cycle_feedback.append(f"ERROR: Pipeline exception: {exc}")

        # Carry feedback forward
        feedback_history.extend(cycle_feedback)

        # Re-project state after storing effects
        timeline, queue_jobs, events = project_state(event_log_path, timeline_path)

        # Save OTIO
        timeline.to_json_file(timeline_path)

        # Build script metadata for VM context
        script_meta = timeline.metadata.get("documentary", {})
        script_meta["has_script"] = bool(script_meta.get("narration_v1"))

        # Answer any questions workers asked before dispatching more work
        await answer_pending_questions(queue_jobs, event_store, brief)

        # Dispatch pending jobs to workers
        await dispatch_pending_jobs(queue_jobs, event_store, output_dir, script_meta)

        # Run QA gates on completed artifacts
        run_qa_gates(queue_jobs, event_store)

        # Try assembly
        if audio_complete and video_complete and not has_output:
            run_assembly(output_dir, queue_jobs)
            # Re-check
            if os.path.exists(output_path):
                print("[PIPELINE] Assembly complete!")
                return f"Pipeline complete in {cycle + 1} cycles."

    return f"Pipeline reached max cycles ({max_cycles})."


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline_v4.py <brief>", file=sys.stderr)
        sys.exit(1)
    brief = " ".join(sys.argv[1:])
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_output")
    result = asyncio.run(run_pipeline(brief, output_dir))
    print(f"\nResult: {result}")
