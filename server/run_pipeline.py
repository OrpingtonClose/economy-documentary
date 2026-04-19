#!/usr/bin/env python3
"""
Direct pipeline runner — bypasses AG-UI/CopilotKit and runs the documentary
pipeline end-to-end using Google ADK's Runner.

Usage:
    cd server
    poetry run python run_pipeline.py \
        --topic "Cloudberry Jam" \
        --corpus /tmp/documentary-pipeline/corpus/cloudberry_research.md \
        --test-mode

Environment:
    ADK_MODEL          — primary model (e.g. openai/google/gemini-2.5-flash)
    OPENAI_API_KEY     — API key for LiteLLM-routed models
    OPENAI_API_BASE    — API base URL (e.g. https://openrouter.ai/api/v1)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# Force simulation mode if --test-mode is passed (must be before imports)
_SIMULATION_MODE = "--test-mode" in sys.argv or os.environ.get(
    "DOCUMENTARY_SIMULATION_MODE", ""
).strip().lower() in ("1", "true")
if "--quick-test" in sys.argv:
    os.environ["DOCUMENTARY_QUICK_TEST"] = "true"

from dotenv import load_dotenv
load_dotenv()

# Activate the E1 (happy path) simulation scenario when --test-mode is used.
# This replaces the old DOCUMENTARY_TEST_MODE env var — the ADK
# EnvironmentSimulationEngine must be explicitly activated with a config.
if _SIMULATION_MODE:
    from testing.simulation_bridge import activate_simulation
    from testing.scenarios import get_scenario
    _sim_scenario = os.environ.get("SIMULATION_SCENARIO", "E1")
    try:
        _sim_config = get_scenario(_sim_scenario)
    except KeyError:
        _sim_config = None
    if _sim_config:
        activate_simulation(_sim_config, scenario_name=_sim_scenario)
    else:
        print(f"ERROR: Unknown simulation scenario '{_sim_scenario}', cannot proceed in test mode")
        sys.exit(1)

from google.adk.agents import SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


class DashboardReporter:
    """Lightweight HTTP reporter that bridges run_pipeline.py to server.py's dashboard.

    Posts status updates to the /dashboard/ingest endpoint so the SSE stream
    can show real-time pipeline progress to the frontend.
    """

    def __init__(self, run_id: str, topic: str, server_url: str = ""):
        self.run_id = run_id
        self.topic = topic
        self.server_url = server_url or os.environ.get(
            "DASHBOARD_SERVER_URL", "http://localhost:8000"
        )
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        # GAP 4.2: Track consecutive failures for escalation
        self._consecutive_failures = 0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._thread.start()
        logger.info("DashboardReporter started -> %s", self.server_url)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def send(self, event_type: str, **kwargs: Any) -> None:
        payload = {
            "run_id": self.run_id,
            "topic": self.topic,
            "event_type": event_type,
            **kwargs,
        }
        with self._lock:
            self._queue.append(payload)

    def _sender_loop(self) -> None:
        """Background thread that drains the queue and POSTs to server."""
        url = f"{self.server_url.rstrip('/')}/dashboard/ingest"
        while self._running or self._queue:
            batch: list[dict] = []
            with self._lock:
                batch, self._queue = self._queue[:], []

            for payload in batch:
                try:
                    data = json.dumps(payload).encode()
                    req = Request(url, data=data, method="POST")
                    req.add_header("Content-Type", "application/json")
                    with urlopen(req, timeout=5) as resp:
                        resp.read()
                except Exception as exc:
                    # GAP 4.2: Escalation on consecutive failures
                    self._consecutive_failures += 1
                    if self._consecutive_failures == 10:
                        logger.error(
                            "DashboardReporter: 10 consecutive POST failures. "
                            "Dashboard may be down. Last error: %s", exc,
                        )
                    elif self._consecutive_failures % 30 == 0:
                        logger.warning(
                            "DashboardReporter: %d consecutive failures. "
                            "Dashboard still unreachable: %s",
                            self._consecutive_failures, exc,
                        )
                    else:
                        logger.debug("DashboardReporter POST failed: %s", exc)
                else:
                    # Reset on success
                    if self._consecutive_failures > 0:
                        logger.info(
                            "DashboardReporter: recovered after %d failures",
                            self._consecutive_failures,
                        )
                    self._consecutive_failures = 0

            time.sleep(1.0)


class ProgressHeartbeat:
    """GAP 6.1: Periodic heartbeat to prevent silent pipeline stalls.

    Emits a log message every ``interval`` seconds with current phase,
    detail, and clip progress.  Ensures the pipeline never goes silent
    for more than a few minutes — any monitoring system watching logs
    will see regular proof-of-life signals.
    """

    def __init__(self, interval: int = 300):
        self.interval = interval  # seconds (default 5 min)
        self.phase: str = "init"
        self.detail: str = ""
        self.clips_done: int = 0
        self.clips_total: int = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        logger.info("ProgressHeartbeat started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def update(self, phase: str = "", detail: str = "",
               clips_done: int = -1, clips_total: int = -1) -> None:
        with self._lock:
            if phase:
                self.phase = phase
            if detail:
                self.detail = detail
            if clips_done >= 0:
                self.clips_done = clips_done
            if clips_total >= 0:
                self.clips_total = clips_total

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(self.interval)
            if not self._running:
                break
            with self._lock:
                logger.info(
                    "HEARTBEAT: phase=%s, detail=%s, clips=%d/%d",
                    self.phase, self.detail,
                    self.clips_done, self.clips_total,
                )


# Module-level heartbeat instance for use by callbacks
_heartbeat: ProgressHeartbeat | None = None


def get_heartbeat() -> ProgressHeartbeat | None:
    """Return the active heartbeat instance (if any)."""
    return _heartbeat


async def run_pipeline(topic: str, corpus_path: str, language: str = "dual_ru_en", quick_test: bool = False) -> dict:
    """Run the full documentary pipeline.

    Args:
        topic: Documentary topic.
        corpus_path: Path to the research corpus file.
        language: Language mode — "en", "ru", or "dual_ru_en".

    Returns:
        Final session state dict.
    """
    # GAP 6.1: Start progress heartbeat
    global _heartbeat
    heartbeat_interval = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))
    _heartbeat = ProgressHeartbeat(interval=heartbeat_interval)
    _heartbeat.start()
    _heartbeat.update(phase="init", detail=f"topic={topic}")

    # Import pipeline agent (triggers model_config resolution)
    from agents.pipeline import pipeline_agent
    from callbacks.state_manager import build_pipeline_state

    # Build initial state
    initial_state = build_pipeline_state()
    initial_state["topic"] = topic
    initial_state["corpus_path"] = corpus_path
    initial_state["language"] = language

    # ARCH-A3 (#133): stage the original user brief so the Preference
    # Ledger R0 seed can parse it into baseline records before the
    # pipeline's first producer agent runs. The actual seeding lives
    # in _init_pipeline_state; staging the text here (rather than the
    # full corpus) keeps R0 records short and traceable to the user's
    # original ask.
    _explicit_brief = os.environ.get("DOCUMENTARY_BRIEF_TEXT", "").strip()
    if _explicit_brief:
        _original_brief = _explicit_brief
    else:
        _original_brief = f"Create an ADHD-friendly documentary about: {topic}"
        if language == "ru":
            _original_brief += " (narration in Russian)"
        elif language == "dual_ru_en":
            _original_brief += " (dual Russian primary / English subtitle narration)"
    from callbacks.run_start_seed import ORIGINAL_BRIEF_KEY
    initial_state[ORIGINAL_BRIEF_KEY] = _original_brief

    # Quick-test mode: inject constraints into state for LLM prompt templates
    if quick_test:
        from agents.scenario_director import _QUICK_TEST_RULES
        initial_state["quick_test"] = "true"
        initial_state["quick_test_rules"] = _QUICK_TEST_RULES
        initial_state["max_scene_duration"] = "15"
        initial_state["max_words_per_scene"] = "37"
        logger.info("QUICK TEST MODE: 2 scenes, ~15s each, ~1 min total")
    else:
        initial_state["quick_test"] = ""
        initial_state["quick_test_rules"] = ""
        initial_state["max_scene_duration"] = "45"
        initial_state["max_words_per_scene"] = "112"

    # Restore from B2 if a previous run exists for this topic
    from tools.b2_checkpoint import restore_pipeline, set_run_id, get_run_id
    os.environ["DOCUMENTARY_TOPIC"] = topic  # used by get_run_id() for new runs
    restored = restore_pipeline(topic)
    stages_complete = restored.get("stages_complete", [])
    if restored["run_id"]:
        logger.info(
            "B2 restored run '%s': stages=%s, files=%d",
            restored["run_id"], stages_complete, restored["restored_files"],
        )
        # Merge restored state into initial state (restored takes precedence)
        for k, v in restored.get("state", {}).items():
            if v and str(v).strip() not in ("", "[]", "{}", "(not yet analyzed)",
                                              "(not yet generated)", "(not yet evaluated)"):
                initial_state[k] = v
    else:
        # New run — generate run_id
        logger.info("No previous B2 run found, starting fresh")

    # Store stages_complete in state so callbacks can skip completed stages
    initial_state["_b2_stages_complete"] = stages_complete

    # Set up ADK session
    session_service = InMemorySessionService()
    runner = Runner(
        agent=pipeline_agent,
        app_name="documentary_pipeline_runner",
        session_service=session_service,
    )

    session = await session_service.create_session(
        app_name="documentary_pipeline_runner",
        user_id="pipeline_runner",
        state=initial_state,
    )

    logger.info("Session created: %s", session.id)
    logger.info("Topic: %s", topic)
    logger.info("Corpus: %s", corpus_path)
    logger.info("Language: %s", language)
    logger.info("Simulation mode: %s", _SIMULATION_MODE)
    logger.info("Model: %s", os.environ.get("ADK_MODEL", "(default)"))

    # Read corpus content to include in the initial message
    corpus_content = ""
    if os.path.exists(corpus_path):
        with open(corpus_path) as f:
            corpus_content = f.read()
        logger.info("Loaded corpus: %d chars", len(corpus_content))

    # Build the initial user message with corpus and language instructions
    language_instruction = ""
    if language == "ru":
        language_instruction = (
            "\n\nIMPORTANT: Generate ALL narration text in RUSSIAN language. "
            "All V1, V2, V3 voice blocks must be in Russian."
        )
    elif language == "dual_ru_en":
        language_instruction = (
            "\n\nIMPORTANT: Generate narration in DUAL LANGUAGE format. "
            "For each voice block, first write the text in RUSSIAN, then provide "
            "an English translation below it. Format:\n"
            "  [RU] <Russian narration text>\n"
            "  [EN] <English translation>\n"
            "Both versions will be used for the documentary — Russian as primary "
            "narration and English as subtitle/alternate track."
        )

    user_message = (
        f"Create an ADHD-friendly documentary about: {topic}\n\n"
        f"Here is the research corpus:\n\n{corpus_content}"
        f"{language_instruction}"
    )

    # Start infra agent for continuous health monitoring.
    # Runs on a daemon thread (not an asyncio task) so that time.sleep()
    # in pipeline callbacks (check_infra_pause) cannot deadlock the
    # monitoring loop.
    from infra_agent import start_infra_agent
    infra = start_infra_agent(poll_interval=30.0, max_consecutive_failures=3)
    infra.start()
    logger.info("InfraAgent started on daemon thread")

    # Start dashboard reporter — bridges this process to server.py's SSE
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    reporter = DashboardReporter(run_id=run_id, topic=topic)
    reporter.start()
    reporter.send("phase_start", phase="scenario")

    # ── Graph-level recovery wrapper (from Strands lessons) ─────────────
    # Wraps the entire pipeline execution with async-native retry logic.
    # On failure, the pipeline is retried with fresh state (restored from
    # B2 checkpoints if available).
    #
    # NOTE: We use an async retry loop instead of recovery.py's
    # execute_with_recovery() because the pipeline is async and ADK's
    # Runner/SessionService/Session are bound to the current event loop.
    # Wrapping in a sync function + thread pool would crash with
    # "attached to a different loop" errors.
    from copy import deepcopy

    _pipeline_max_retries = int(os.environ.get("PIPELINE_MAX_RETRIES", "1"))
    _retry_backoff_base = 5.0
    _retry_backoff_max = 30.0

    # Snapshot initial state for clean retries
    _initial_state_snapshot = deepcopy(initial_state)

    start_time = time.time()
    logger.info("Starting pipeline run...")

    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=user_message)],
    )

    final_response = None
    _last_phase = "scenario"

    async def _run_pipeline_once() -> str:
        """Execute one full pipeline run.  Returns final response text.

        On retry, the session state is reset from the snapshot (which may
        include B2-restored checkpoints, so completed stages are skipped).
        """
        nonlocal final_response, _last_phase
        _last_phase = "scenario"

        # Reset session state from snapshot for clean retry
        current_session = await session_service.get_session(
            app_name="documentary_pipeline_runner",
            user_id="pipeline_runner",
            session_id=session.id,
        )
        if current_session and hasattr(current_session, "state"):
            for k, v in _initial_state_snapshot.items():
                current_session.state[k] = v

        async for event in runner.run_async(
            user_id="pipeline_runner",
            session_id=session.id,
            new_message=content,
        ):
            if event.content and event.content.parts:
                text = "".join(p.text or "" for p in event.content.parts if hasattr(p, "text"))
                if text.strip():
                    agent_name = event.author or "unknown"
                    logger.info("[%s] %s", agent_name, text)
                    final_response = text

                    # Track phase transitions for dashboard
                    lower = text.lower()
                    for phase in ("audio", "visual_direction", "production", "assembly"):
                        if phase.replace("_", " ") in lower or phase in lower:
                            if phase != _last_phase:
                                reporter.send("phase_end", phase=_last_phase, status="completed")
                                reporter.send("phase_start", phase=phase)
                                _last_phase = phase
                                break

                    reporter.send("llm_end", agent=agent_name, duration=0.0, output_tokens=len(text) // 3)

        return final_response or ""

    # Async retry loop — runs in the same event loop as ADK objects
    _retryable = (RuntimeError, ConnectionError, TimeoutError, OSError)
    _last_err: Exception | None = None
    try:
        for attempt in range(_pipeline_max_retries + 1):
            try:
                await _run_pipeline_once()
                _last_err = None
                break
            except _retryable as err:
                _last_err = err
                if attempt < _pipeline_max_retries:
                    backoff = min(
                        _retry_backoff_max,
                        _retry_backoff_base * (2 ** attempt),
                    )
                    logger.warning(
                        "attempt=<%d/%d>, error=<%s> | pipeline failed, "
                        "retrying in %.1fs from B2 checkpoint",
                        attempt + 1, _pipeline_max_retries + 1, err, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "attempt=<%d/%d>, error=<%s> | pipeline exhausted "
                        "all retries",
                        attempt + 1, _pipeline_max_retries + 1, err,
                    )
        if _last_err is not None:
            raise _last_err
    except Exception as pipeline_err:
        logger.error(
            "Pipeline failed after recovery attempts: %s", pipeline_err,
        )
        raise
    finally:
        reporter.send("phase_end", phase=_last_phase, status="completed")
        reporter.send("finalize", status="completed")
        reporter.stop()
        # GAP 6.1: Stop heartbeat
        if _heartbeat:
            _heartbeat.update(phase="complete")
            _heartbeat.stop()
        # Shutdown infra agent regardless of how the pipeline exits
        infra.shutdown()
        logger.info("InfraAgent stopped. Final status: %s", infra.get_worker_summary())

    elapsed = time.time() - start_time
    logger.info("Pipeline completed in %.1f seconds", elapsed)

    # Get final session state
    final_session = await session_service.get_session(
        app_name="documentary_pipeline_runner",
        user_id="pipeline_runner",
        session_id=session.id,
    )

    if final_session:
        raw = final_session.state
        state = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
    else:
        state = {}
    state["_elapsed_sec"] = round(elapsed, 1)
    state["_final_response"] = final_response

    return state


def main():
    parser = argparse.ArgumentParser(description="Run the documentary pipeline directly")
    parser.add_argument("--topic", required=True, help="Documentary topic")
    parser.add_argument("--corpus", required=True, help="Path to research corpus file")
    parser.add_argument("--language", default="dual_ru_en",
                        choices=["en", "ru", "dual_ru_en"],
                        help="Language mode (default: dual_ru_en)")
    parser.add_argument("--test-mode", action="store_true",
                        help="Run in test mode (synthetic media, no GPU needed)")
    parser.add_argument("--quick-test", action="store_true",
                        help="Quick test mode: 2 scenes, ~15s each, ~1 min total movie")
    parser.add_argument("--output-dir", default="/tmp/documentary-pipeline/output",
                        help="Output directory for the final documentary")
    args = parser.parse_args()

    # Ensure output dirs exist
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs("/tmp/documentary-pipeline/timelines", exist_ok=True)
    os.makedirs("/tmp/documentary-pipeline/audio", exist_ok=True)
    os.makedirs("/tmp/documentary-pipeline/video", exist_ok=True)
    os.makedirs("/tmp/documentary-pipeline/assembly", exist_ok=True)

    # Ensure B2 credentials are available
    if not os.environ.get("B2_KEY_ID"):
        logger.warning("B2_KEY_ID not set -- B2 checkpointing will be disabled")
    if not os.environ.get("B2_APPLICATION_KEY"):
        logger.warning("B2_APPLICATION_KEY not set -- B2 checkpointing will be disabled")

    logger.info("=== Documentary Pipeline Runner ===")

    # ── Pre-flight checks ─────────────────────────────────────────
    # ARCHITECTURE INVARIANT: Every required worker must be healthy
    # before the pipeline starts.  Never silently degrade to
    # synthetic/placeholder media — that wastes hours of GPU time on
    # downstream stages that depend on real upstream artifacts.
    if not args.test_mode and not _SIMULATION_MODE:
        _preflight_check_dashboard()  # GAP 4.1
        _preflight_check_workers()

    result = asyncio.run(run_pipeline(
        topic=args.topic,
        corpus_path=args.corpus,
        language=args.language,
        quick_test=args.quick_test,
    ))

    # Print summary
    print("\n" + "=" * 60)
    print("PIPELINE RESULTS")
    print("=" * 60)
    print(f"Topic: {result.get('topic', 'unknown')}")
    print(f"Language: {result.get('language', 'unknown')}")
    print(f"Pipeline phase: {result.get('pipeline_phase', 'unknown')}")
    print(f"Elapsed: {result.get('_elapsed_sec', 0)}s")
    print(f"Timeline path: {result.get('_timeline_path', 'none')}")

    # Print scenes summary
    scenes_raw = result.get("scenes", "[]")
    try:
        scenes = json.loads(scenes_raw) if isinstance(scenes_raw, str) else scenes_raw
        if isinstance(scenes, list):
            print(f"Scenes: {len(scenes)}")
            for s in scenes:
                if isinstance(s, dict):
                    print(f"  Scene {s.get('scene_num', '?')}: {s.get('title', 'untitled')}")
    except (json.JSONDecodeError, TypeError):
        print(f"Scenes (raw): {str(scenes_raw)}")

    # Save full state
    output_path = os.path.join(args.output_dir, "pipeline_state.json")
    try:
        with open(output_path, "w") as f:
            # Filter out non-serializable values
            serializable = {}
            for k, v in result.items():
                try:
                    json.dumps(v)
                    serializable[k] = v
                except (TypeError, ValueError):
                    serializable[k] = str(v)
            json.dump(serializable, f, indent=2, ensure_ascii=False)
        print(f"\nFull state saved to: {output_path}")
    except Exception as e:
        print(f"\nFailed to save state: {e}")

    print("=" * 60)


def _preflight_check_dashboard() -> None:
    """GAP 4.1: Verify the dashboard is reachable before pipeline start.

    If DASHBOARD_URL is set, checks the /health endpoint. Exits with error
    if unreachable (unless SKIP_DASHBOARD_CHECK=1).
    """
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if not dashboard_url:
        logger.info("DASHBOARD_URL not set — skipping dashboard pre-flight check")
        return

    if os.environ.get("SKIP_DASHBOARD_CHECK", "").lower() in ("1", "true"):
        logger.warning("SKIP_DASHBOARD_CHECK is set — skipping dashboard pre-flight check")
        return

    health_url = f"{dashboard_url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != "ok":
            logger.error(
                "PRE-FLIGHT FAILED: Dashboard at %s reports unhealthy: %s",
                dashboard_url, data,
            )
            sys.exit(1)
        logger.info("PRE-FLIGHT OK: Dashboard at %s is healthy", dashboard_url)
    except Exception as exc:
        logger.error(
            "PRE-FLIGHT FAILED: Dashboard at %s is unreachable: %s. "
            "Set SKIP_DASHBOARD_CHECK=1 to bypass this check.",
            dashboard_url, exc,
        )
        sys.exit(1)


class PreflightError(RuntimeError):
    """Raised when a pre-flight worker health check fails."""


def _check_worker(name: str, url: str, expected_capability: str) -> None:
    """Verify a single GPU worker is reachable and has the expected model loaded.

    Raises ``PreflightError`` if the worker is unreachable or the required
    model is not loaded.  This enforces the architecture invariant:
    **every required service must be confirmed healthy before pipeline start.**
    """
    health_url = f"{url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        msg = f"PRE-FLIGHT FAILED: {name} worker at {url} is unreachable: {exc}"
        logger.error(msg)
        raise PreflightError(msg) from exc

    if data.get("status") != "ok":
        msg = (
            f"PRE-FLIGHT FAILED: {name} worker at {url} reports "
            f"unhealthy status: {data}"
        )
        logger.error(msg)
        raise PreflightError(msg)

    # Check that the required model is actually loaded
    loaded_key = f"{expected_capability}_loaded"
    if not data.get(loaded_key, False):
        msg = (
            f"PRE-FLIGHT FAILED: {name} worker at {url} does not have "
            f"{expected_capability} loaded. Health response: {data}. "
            f"Each model MUST run on its own dedicated VM — never swap or share models."
        )
        logger.error(msg)
        raise PreflightError(msg)

    vram_gb = data.get("vram_used_gb", "?")
    vram_total = data.get("vram_total_gb", "?")
    logger.info(
        "PRE-FLIGHT OK: %s worker at %s — %s loaded, VRAM %s/%s GB",
        name, url, expected_capability, vram_gb, vram_total,
    )


def _preflight_check_workers() -> None:
    """Validate that ALL required GPU workers are healthy before pipeline start.

    Architecture invariants enforced:
    1. TTS worker must be reachable and have TTS model loaded.
    2. At least one video worker must be reachable and have LTX loaded.
    3. Each model runs on its own dedicated VM — never shared.

    If any check fails the pipeline exits immediately with a clear error
    message instead of silently degrading to synthetic/placeholder media.
    """
    logger.info("=== Pre-flight worker checks ===")

    # -- TTS worker (required: real narration drives all downstream timing) --
    tts_url = os.environ.get("TTS_WORKER_URL", "")
    if not tts_url:
        logger.error(
            "PRE-FLIGHT FAILED: TTS_WORKER_URL is not set. "
            "A dedicated TTS worker VM is REQUIRED — the pipeline cannot start "
            "without real narration because all video timing depends on it. "
            "Provision a TTS VM and set TTS_WORKER_URL before restarting."
        )
        sys.exit(1)
    try:
        _check_worker("TTS", tts_url, "tts")
    except PreflightError:
        sys.exit(1)

    # -- Video workers (at least one required) --
    video_urls_str = os.environ.get("VIDEO_WORKER_URLS", "")
    gpu_url = os.environ.get("GPU_WORKER_URL", "")
    video_urls = [u.strip() for u in video_urls_str.split(",") if u.strip()] if video_urls_str else []
    if gpu_url and gpu_url not in video_urls:
        video_urls.append(gpu_url)

    if not video_urls:
        logger.error(
            "PRE-FLIGHT FAILED: No video worker URLs configured. "
            "Set VIDEO_WORKER_URLS or GPU_WORKER_URL to at least one "
            "LTX-dedicated GPU worker VM."
        )
        sys.exit(1)

    healthy_video = 0
    for vurl in video_urls:
        try:
            _check_worker("Video", vurl, "ltx")
            healthy_video += 1
        except PreflightError:
            logger.warning("Video worker %s failed pre-flight, skipping", vurl)

    if healthy_video == 0:
        logger.error(
            "PRE-FLIGHT FAILED: No healthy video workers found. "
            "At least one LTX-dedicated GPU VM must be running."
        )
        sys.exit(1)

    logger.info(
        "=== Pre-flight PASSED: TTS worker OK, %d/%d video workers OK ===",
        healthy_video, len(video_urls),
    )


if __name__ == "__main__":
    main()
