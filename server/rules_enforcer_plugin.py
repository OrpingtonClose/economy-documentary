#!/usr/bin/env python3
import os
import sys
import json
import httpx

# Path to the DeepSeek API key on the host
DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"

# Extract rules from project documentation
RULES_PROMPT = """
You are the Antigravity Code Quality Enforcer for this project.
Your task is to analyze the content of the modified file and verify compliance with the following core architectural rules:

1. NO TIMEOUTS IN CODE:
   - There must be absolutely no `timeout=...` arguments in urllib, requests, httpx, or any HTTP calls in non-health-probe code.
   - There must be no `asyncio.timeout`, `setTimeout`, `threading.Timer`, or timer primitives.
   - There must be no fixed polling loops (e.g. `time.sleep(X)` inside a while or for loop) without clear, documented justification or dynamic backoff.
   - No timeout-based client self-destruction logic.

2. DYNAMIC PATH ROUTING FOR GSA:
   - GSA (Global State Agent) routes must query runs using dynamic path parameters: `/runs/{run_id}`.
   - No query string parameters (specifically `?run_id=...`) are allowed for run identification on the GSA GET state endpoints.
   - All client calls from agents and tests query GSA via the path format `/runs/{run_id}` rather than `?run_id=...`.

3. REAL ENGINES ONLY:
   - No mocks, stubs, or simulation layers for TTS (must use Qwen3-TTS), Video (must use LTX-2.3), or LLM inference.
   - Simulated/mocked components in production files are strictly prohibited.

4. NATURAL LANGUAGE ONLY FOR AGENTS:
   - Agents must output free-form prose and must not emit structured outputs (e.g., JSON, XML, tagged sections, or `EFFECT:` labels).
   - All extraction complexity must remain inside the category-conditioned parser, never in the agent prompt or output format.

Review the file path and file content below and determine if any of these rules have been violated.
If there are violations, detail them clearly.
You MUST respond in JSON format with the following keys:
{
  "status": "PASS" or "FAIL",
  "violations": [
    "Violation 1 description with file and line if visible",
    "Violation 2..."
  ]
}
"""

def main():
    if len(sys.argv) < 2:
        print("✅ Antigravity Plugin: No file argument passed. PASS.")
        sys.exit(0)

    file_path = sys.argv[1]

    # Skip checking the enforcer plugin itself
    if "rules_enforcer_plugin.py" in file_path:
        print("✅ Antigravity Plugin: Skipping self-check. PASS.")
        sys.exit(0)

    if not os.path.exists(file_path):
        print(f"✅ Antigravity Plugin: File {file_path} does not exist (possibly deleted). PASS.")
        sys.exit(0)

    # Read API Key
    if not os.path.exists(DEEPSEEK_KEY_PATH):
        print(f"⚠️ Antigravity Plugin: API key file not found at {DEEPSEEK_KEY_PATH}. Skipping checks.")
        sys.exit(0)

    with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
        api_key = f.read().strip()

    if not api_key:
        print("⚠️ Antigravity Plugin: Empty API key. Skipping checks.")
        sys.exit(0)

    print(f"🔍 Antigravity Plugin: Analyzing file {file_path} using DeepSeek...")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"⚠️ Antigravity Plugin: Could not read {file_path}: {e}")
        sys.exit(0)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": RULES_PROMPT},
            {"role": "user", "content": f"File Path: {file_path}\n\nFile Content:\n```python\n{content}\n```"}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}
    }

    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            print(f"❌ Antigravity Plugin: DeepSeek API error (status {resp.status_code}): {resp.text}")
            sys.exit(1)

        result = json.loads(resp.json()["choices"][0]["message"]["content"])
        if result.get("status") == "FAIL":
            print("\n❌ ANTIGRAVITY RULES VIOLATION DETECTED!")
            for v in result.get("violations", []):
                print(f" - {v}")
            print("\nPlease revert or fix the rule violations.\n")
            sys.exit(1)
        else:
            print("✅ Antigravity Plugin: File conforms to core principles. PASS.")
            sys.exit(0)
    except Exception as e:
        print(f"❌ Antigravity Plugin: Verification failed for {file_path}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
