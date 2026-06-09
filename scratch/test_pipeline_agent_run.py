import os
import sys
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import get_agent_model, ROLE_INSTRUCTIONS, otio_aware_compress, PipelineDeps, get_local_mem0
from pydantic_deep import create_deep_agent, DeepAgentDeps, PeriodicReminderConfig, create_sliding_window_processor
from pydantic_ai_provenance.capability import ProvenanceCapability
from pydantic_ai_summarization import ContextManagerCapability
from pydantic_ai_shields import CostTracking

async def main():
    print("Getting agent model...")
    model = get_agent_model()
    print("Creating custom agent...")
    
    provenance = ProvenanceCapability(
        agent_name="scenario",
        source_tools=["bash_command"],
    )
    caps = [
        provenance,
        ContextManagerCapability(
            max_tokens=128000,
        ),
        CostTracking(budget_usd=10.0),
    ]
    
    agent = create_deep_agent(
        model=model,
        instructions=ROLE_INSTRUCTIONS["scenario"],
        on_before_compress=otio_aware_compress,
        history_processors=[
            create_sliding_window_processor(
                trigger=("messages", 100),
                keep=("messages", 50),
                max_input_tokens=128000,
            ),
        ],
        eviction_token_limit=None,
        context_manager=True,
        context_manager_max_tokens=128000,
        include_todo=False,
        include_filesystem=False,
        include_plan=False,
        include_memory=False,
        include_checkpoints=False,
        web_search=False,
        web_fetch=False,
        include_skills=False,
        include_subagents=False,
        include_builtin_subagents=False,
        thinking=False,
        cost_tracking=True,
        cost_budget_usd=10.0,
        stuck_loop_detection=True,
        periodic_reminder=PeriodicReminderConfig(every_n_turns=10, first_after=5),
        capabilities=caps,
        deps_type=DeepAgentDeps,
    )
    
    print("Running agent...")
    deps = DeepAgentDeps(gsa_url="http://127.0.0.1:65001/", agent_role="scenario", compaction_model=model)
    try:
        result = await agent.run("Say hello", deps=deps)
        print("Agent output:", result.output)
    except Exception as e:
        print("Agent error:", e)

if __name__ == "__main__":
    asyncio.run(main())
