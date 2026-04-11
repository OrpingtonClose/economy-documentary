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
import time

# Force test mode if --test-mode is passed (must be before imports)
if "--test-mode" in sys.argv:
    os.environ["DOCUMENTARY_TEST_MODE"] = "true"

from dotenv import load_dotenv
load_dotenv()

from google.adk.agents import SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def run_pipeline(topic: str, corpus_path: str, language: str = "dual_ru_en") -> dict:
    """Run the full documentary pipeline.

    Args:
        topic: Documentary topic.
        corpus_path: Path to the research corpus file.
        language: Language mode — "en", "ru", or "dual_ru_en".

    Returns:
        Final session state dict.
    """
    # Import pipeline agent (triggers model_config resolution)
    from agents.pipeline import pipeline_agent
    from callbacks.state_manager import build_pipeline_state

    # Build initial state
    initial_state = build_pipeline_state()
    initial_state["topic"] = topic
    initial_state["corpus_path"] = corpus_path
    initial_state["language"] = language

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
    logger.info("Test mode: %s", os.environ.get("DOCUMENTARY_TEST_MODE", "false"))
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

    # Run the pipeline
    start_time = time.time()
    logger.info("Starting pipeline run...")

    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=user_message)],
    )

    final_response = None
    async for event in runner.run_async(
        user_id="pipeline_runner",
        session_id=session.id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts if hasattr(p, "text"))
            if text.strip():
                agent_name = event.author or "unknown"
                # Truncate for logging
                preview = text[:200] + "..." if len(text) > 200 else text
                logger.info("[%s] %s", agent_name, preview)
                final_response = text

    elapsed = time.time() - start_time
    logger.info("Pipeline completed in %.1f seconds", elapsed)

    # Get final session state
    final_session = await session_service.get_session(
        app_name="documentary_pipeline_runner",
        user_id="pipeline_runner",
        session_id=session.id,
    )

    state = dict(final_session.state) if final_session else {}
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

    result = asyncio.run(run_pipeline(
        topic=args.topic,
        corpus_path=args.corpus,
        language=args.language,
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
        print(f"Scenes (raw): {str(scenes_raw)[:200]}")

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


if __name__ == "__main__":
    main()
