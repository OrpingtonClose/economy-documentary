import sys
import os
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "server"))

from effect_parser import parse_agent_text_multi

# Load the text from the debug log
with open('/tmp/documentary-pipeline/agent_debug_scenario.log') as f:
    log_content = f.read()

# The response starts after the second TURN START block
# Let's split by the separator
parts = log_content.split('RESPONSE:\n')
if len(parts) >= 2:
    agent_text = parts[-1].split('========================================')[0].strip()
else:
    agent_text = log_content

print("--- AGENT TEXT ---")
print(agent_text[:500])
print("...")
print("------------------")

async def main():
    # Set mock LOG_DIR if needed
    os.environ["DATA_DIR"] = "/tmp/documentary-pipeline"
    
    effects = await parse_agent_text_multi("scenario", agent_text)
    print("\n--- EXTRACTED EFFECTS ---")
    for eff in effects:
        print(f"Kind: {eff.kind}")
        print(f"Data: {eff.model_dump()}")

asyncio.run(main())
