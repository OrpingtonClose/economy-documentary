"# Pipeline Corrections Review

This document logs issues discovered during the initial run of the V7.1 pipeline architecture and outlines proposed corrections for later review.

## 1. Scenario Agent Autonomous Loop Deadlock

*   **Location**: `server/agent_base.py` (autonomous polling loop)
*   **Issue**: The Scenario Agent's autonomous loop triggers whenever `current_phase == "init"`. When a run is first initialized, the timeline has no slots. Without an initial topic instruction, the Scenario Agent runs autonomously, queries the GSA, finds no slots, and keeps calling `bash_command` (curling GSA or checking the filesystem) in a loop trying to find what to do. This acquires the uvicorn lock, causing the pipeline runner's initial `POST` request (which carries the actual topic instruction) to block and eventually time out after 120 seconds.
*   **Proposed Correction**: Update the autonomous trigger for the Scenario Agent to only fire if there is an existing script (slots) to refine:
    ```python
    if role == "scenario":
        slots = state.get("otio", {}).get("slots", {})
        unfilled = any(s.get("status") == "scripted" for s in slots.values())
        if slots and (current_phase == "init" or unfilled):
            should_act = True
    ```

## 2. Default Usage Limit Halts Agents

*   **Location**: `server/agent_base.py` (`execute_agent_turn`)
*   **Issue**: `pydantic-ai` defaults to a `request_limit` of 50. During complex agent reasoning loops or long-running execution sequences, the agent can exceed 50 tool requests and raise `UsageLimitExceeded`, halting the pipeline.
*   **Proposed Correction**: Explicitly import and pass a higher usage limit to the `agent.run` call:
    ```python
    from pydantic_ai import UsageLimits
    result = await agent.run(prompt, deps=deps, usage_limits=UsageLimits(request_limit=300))
    ```

## 3. Thinking Mode Delay on Non-Reasoning Models

*   **Location**: `server/agent_base.py` (`create_pipeline_agent`)
*   **Issue**: `thinki
<truncated 2052 bytes>