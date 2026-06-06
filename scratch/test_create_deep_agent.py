import os
import sys
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import get_agent_model, create_pipeline_agent

def main():
    print("Getting agent model...")
    model = get_agent_model()
    print("Calling create_pipeline_agent...")
    agent = create_pipeline_agent("scenario", model)
    print("create_pipeline_agent completed successfully!")

if __name__ == "__main__":
    main()
