#!/usr/bin/env python3
"""
Direct pipeline runner — bypasses AG-UI/CopilotKit and runs the documentary
pipeline end-to-end using Strands Agents GraphBuilder.

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

# Force test mode if --test-mode is passed (must be before imports)
if "--test-mode" in sys.argv:
    os.environ["DOCUMENTARY_TEST_MODE"] = "true"
if "--quick-test" in sys.argv:
    os.environ["DOCUMENTARY_QUICK_TEST"] = "true"

from dotenv import load_dotenv
load_dotenv()

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
                    if self._consecutive_failures > 0:
                        logger.info(
                            "DashboardReporter: recovered after %d failures",
                            self._consecutive_failures,
                        )
                    self._consecutive_failures = 0

            time.sleep(1.0)


class ProgressHeartbeat:
    """Periodic heartbeat to prevent silent pipeline stalls.

    Emits a log message every ``interval`` seconds with current phase,
    detail, and clip progress.
    """

    def __init__(self, interval: int = 300):
        self.interval = interval
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


# Thread-local heartbeat storage — each concurrent run_pipeline() call gets
# its own ProgressHeartbeat, preventing cross-request overwrites.
_heartbeat_local = threading.local()


def get_heartbeat() -> ProgressHeartbeat | None:
    """Return the active heartbeat instance for the current thread (if any)."""
    return getattr(_heartbeat_local, "heartbeat", None)


def run_pipeline(topic: str, corpus_path: str, language: str = "dual_ru_en", quick_test: bool = False) -> dict:
    """Run the full documentary pipeline using Strands Graph.

    Args:
        topic: Documentary topic.
        corpus_path: Path to the research corpus file.
        language: Language mode — "en", "ru", or "dual_ru_en".
        quick_test: If True, run in quick test mode (2 scenes, ~15s each).

    Returns:
        Final pipeline state dict.
    """
    # Import and build pipeline graph FIRST — before starting any threads.
    # If build_pipeline() raises (e.g. import error in agent module), no
    # heartbeat/infra/reporter threads are leaked.
    from agents.pipeline_graph import build_pipeline

    heartbeat_interval = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))
    heartbeat = ProgressHeartbeat(interval=heartbeat_interval)
    _heartbeat_local.heartbeat = heartbeat
    heartbeat.start()
    heartbeat.update(phase="init", detail=f"topic={topic}")

    # Build initial state
    initial_state: dict[str, Any] = {}
    initial_state["topic"] = topic
    initial_state["corpus_path"] = corpus_path
    initial_state["language"] = language
    initial_state["pipeline_phase"] = "scenario"

    # Quick-test mode constraints
    if quick_test:
        initial_state["quick_test"] = "true"
        initial_state["max_scene_duration"] = "15"
        initial_state["max_words_per_scene"] = "37"
        logger.info("QUICK TEST MODE: 2 scenes, ~15s each, ~1 min total")
    else:
        initial_state["quick_test"] = ""
        initial_state["max_scene_duration"] = "45"
        initial_state["max_words_per_scene"] = "112"

    # Restore from B2 if a previous run exists for this topic
    try:
        from tools.b2_checkpoint import restore_pipeline, set_run_id
        # Generate a thread-safe run ID instead of mutating os.environ
        import time as _time
        safe_topic = "".join(c if c.isalnum() else "_" for c in topic.lower())[:30]
        set_run_id(f"{safe_topic}_{int(_time.time())}")
        restored = restore_pipeline(topic)
        stages_complete = restored.get("stages_complete", [])
        if restored["run_id"]:
            logger.info(
                "B2 restored run '%s': stages=%s, files=%d",
                restored["run_id"], stages_complete, restored["restored_files"],
            )
            for k, v in restored.get("state", {}).items():
                if v and str(v).strip() not in ("", "[]", "{}", "(not yet analyzed)",
                                                  "(not yet generated)", "(not yet evaluated)"):
                    initial_state[k] = v
        else:
            logger.info("No previous B2 run found, starting fresh")
        initial_state["_b2_stages_complete"] = stages_complete
    except ImportError:
        logger.warning("B2 checkpoint not available, starting fresh")

    # Read corpus content
    corpus_content = ""
    if os.path.exists(corpus_path):
        with open(corpus_path) as f:
            corpus_content = f.read()
        logger.info("Loaded corpus: %d chars", len(corpus_content))

    # Build language instruction
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
            "Both versions will be used for the documentary."
        )

    quick_test_instruction = ""
    if quick_test:
        quick_test_instruction = (
            "\n\nQUICK TEST MODE: Generate EXACTLY 2 scenes only. "
            "Each scene should be ~15 seconds of narration (~37 words per voice block). "
            "Total video target: ~1 minute."
        )

    user_message = (
        f"Create an ADHD-friendly documentary about: {topic}\n\n"
        f"Here is the research corpus:\n\n{corpus_content}"
        f"{language_instruction}"
        f"{quick_test_instruction}"
    )

    # Build pipeline graph BEFORE starting long-lived resources so that if
    # build_pipeline() raises, we don't leak infra/reporter/heartbeat threads.
    pipeline = build_pipeline()

    # Start infra agent for continuous health monitoring
    try:
        from infra_agent import start_infra_agent
        infra = start_infra_agent(poll_interval=30.0, max_consecutive_failures=3)
        infra.start()
        logger.info("InfraAgent started on daemon thread")
    except ImportError:
        infra = None
        logger.warning("InfraAgent not available")

    # Start dashboard reporter
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    reporter = DashboardReporter(run_id=run_id, topic=topic)
    reporter.start()
    reporter.send("phase_start", phase="scenario")

    # Run the pipeline graph
    start_time = time.time()
    logger.info("Starting pipeline run...")

    # Pass initial_state as invocation_state — the graph mutates it in place
    # so we can read the final pipeline state from it after execution.
    # Wrap with graph-level graduated recovery (RETRY → ENVIRONMENTAL → HUMAN)
    # so transient failures get retried before escalating.
    #
    # IMPORTANT: The graph mutates initial_state in place. On retry, we must
    # reset to a clean snapshot so the new attempt doesn't inherit contaminated
    # state from the failed run (e.g., stale scenes, partial timeline paths).
    import copy

    state_snapshot = copy.deepcopy(initial_state)
    # Keep a reference to the last attempt's state so that if all retries
    # fail, we still return partial progress (scenes generated, audio files
    # created, etc.) rather than the blank initial snapshot.
    last_attempt_state: dict[str, Any] = {}

    try:
        from recovery import RecoveryPolicy, execute_with_recovery

        def _run_graph(**kwargs: Any) -> Any:
            # Save reference to last attempt's state before resetting
            last_attempt_state.update(initial_state)
            # Reset initial_state to clean snapshot before each attempt
            initial_state.clear()
            initial_state.update(copy.deepcopy(state_snapshot))
            return pipeline(user_message, invocation_state=initial_state)

        result = execute_with_recovery(
            operation=_run_graph,
            operation_name="documentary_pipeline",
            kwargs={},
            policy=RecoveryPolicy(
                max_retries=1,
                creative_budget=0,
                enable_env_assessment=True,
                escalate_to_human=True,
            ),
        )
        logger.info("Pipeline graph execution completed")
    except ImportError:
        logger.warning("recovery module not available, running pipeline without graph-level recovery")
        try:
            result = pipeline(user_message, invocation_state=initial_state)
            logger.info("Pipeline graph execution completed")
        except Exception as exc:
            logger.exception("Pipeline execution failed")
            result = None
    except Exception as exc:
        logger.exception("Pipeline execution failed after recovery attempts")
        # Restore partial progress from last attempt so caller can inspect
        # what work was completed (scenes, audio, etc.) before failure.
        if last_attempt_state:
            initial_state.clear()
            initial_state.update(last_attempt_state)
        result = None
    finally:
        reporter.send("phase_end", phase="assembly", status="completed")
        reporter.send("finalize", status="completed")
        reporter.stop()
        if heartbeat:
            heartbeat.update(phase="complete")
            heartbeat.stop()
            _heartbeat_local.heartbeat = None
        if infra:
            infra.shutdown()
            logger.info("InfraAgent stopped. Final status: %s", infra.get_worker_summary())

    elapsed = time.time() - start_time
    logger.info("Pipeline completed in %.1f seconds", elapsed)

    # initial_state was mutated by the graph during execution — it now
    # contains the final pipeline state (scenes, timeline, alignment, etc.)
    initial_state["_elapsed_sec"] = round(elapsed, 1)
    initial_state["_result"] = str(result) if result else None

    return initial_state


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

    # Pre-flight checks (production only)
    if not args.test_mode:
        _preflight_check_dashboard()
        _preflight_check_workers()

    result = run_pipeline(
        topic=args.topic,
        corpus_path=args.corpus,
        language=args.language,
        quick_test=args.quick_test,
    )

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
    """Verify the dashboard is reachable before pipeline start."""
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
    """Verify a single GPU worker is reachable and has the expected model loaded."""
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

    loaded_key = f"{expected_capability}_loaded"
    if not data.get(loaded_key, False):
        msg = (
            f"PRE-FLIGHT FAILED: {name} worker at {url} does not have "
            f"{expected_capability} loaded. Health response: {data}. "
            f"Each model MUST run on its own dedicated VM."
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
    """Validate that ALL required GPU workers are healthy before pipeline start."""
    logger.info("=== Pre-flight worker checks ===")

    tts_url = os.environ.get("TTS_WORKER_URL", "")
    if not tts_url:
        logger.error(
            "PRE-FLIGHT FAILED: TTS_WORKER_URL is not set. "
            "A dedicated TTS worker VM is REQUIRED."
        )
        sys.exit(1)
    try:
        _check_worker("TTS", tts_url, "tts")
    except PreflightError:
        sys.exit(1)

    video_urls_str = os.environ.get("VIDEO_WORKER_URLS", "")
    gpu_url = os.environ.get("GPU_WORKER_URL", "")
    video_urls = [u.strip() for u in video_urls_str.split(",") if u.strip()] if video_urls_str else []
    if gpu_url and gpu_url not in video_urls:
        video_urls.append(gpu_url)

    if not video_urls:
        logger.error(
            "PRE-FLIGHT FAILED: No video worker URLs configured. "
            "Set VIDEO_WORKER_URLS or GPU_WORKER_URL."
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
            "PRE-FLIGHT FAILED: No healthy video workers found."
        )
        sys.exit(1)

    logger.info(
        "=== Pre-flight PASSED: TTS worker OK, %d/%d video workers OK ===",
        healthy_video, len(video_urls),
    )


if __name__ == "__main__":
    main()
