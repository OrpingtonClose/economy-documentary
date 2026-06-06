import os
import sys
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import get_agent_model
from pydantic_ai import Agent

async def main():
    print("Getting agent model...")
    model = get_agent_model()
    print("Creating Agent...")
    agent = Agent(model, system_prompt="You are a helpful assistant.")
    print("Running agent...")
    try:
        result = await agent.run("Say 'hello' and nothing else.")
        print("Agent output:", result.data)
    except Exception as e:
        print("Agent error:", e)

if __name__ == "__main__":
    asyncio.run(main())
