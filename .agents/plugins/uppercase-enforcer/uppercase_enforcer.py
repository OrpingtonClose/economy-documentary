#!/usr/bin/env python3
import os
import sys
import json
import re
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime

DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
BRAIN_DIR = Path("/Users/orpington/.gemini/antigravity/brain")

def clean_arg(val):
    if not isinstance(val, str):
        return val
    val = val.strip()
    if val.startswith('"') and val.endswith('"'):
        try:
            val = json.loads(val)
        except Exception:
            pass
    elif val.startswith("'") and val.endswith("'"):
        val = val[1:-1]
    # Unescape common JSON characters
    val = val.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
    return val.strip()

def find_active_transcript():
    if not BRAIN_DIR.exists():
        return None
    newest_transcript = None
    newest_mtime = 0
    for subdir in BRAIN_DIR.iterdir():
        if subdir.is_dir():
            transcript_file = subdir / ".system_generated" / "logs" / "transcript.jsonl"
            if transcript_file.exists():
                try:
                    mtime = transcript_file.stat().st_mtime
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        newest_transcript = transcript_file
                except Exception:
                    pass
    return newest_transcript

def extract_user_prompts(transcript_path):
    prompts = []
    if not transcript_path or not transcript_path.exists():
        return prompts
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                if data.get("source") == "USER_EXPLICIT" and data.get("type") == "USER_INPUT":
                    content = data.get("content") or ""
                    # Extract content inside <USER_REQUEST>...</USER_REQUEST> if present
                    match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                    if match:
                        prompts.append(match.group(1).strip())
                    else:
                        prompts.append(content.strip())
            except Exception:
                continue
    return prompts

def is_caps_lock_prompt(prompt):
    if not prompt:
        return False
    # Check if the prompt is in all caps (ignoring non-alphabetic chars)
    words = re.findall(r"\b[a-zA-Z]{3,}\b", prompt)
    if not words:
        return False
    return all(w.isupper() for w in words)

def find_proposed_tool_call(transcript_path, tool_name, command_arg=None, file_arg=None):
    if not transcript_path or not transcript_path.exists():
        return None
    planner_responses = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                if data.get("source") == "MODEL" and data.get("type") == "PLANNER_RESPONSE":
                    planner_responses.append(data)
            except Exception:
                continue
    if not planner_responses:
        return None
    latest = planner_responses[-1]
    tool_calls = latest.get("tool_calls") or []
    
    # Match tool name or tool name without namespace prefix (e.g. default_api:run_command vs run_command)
    matching_calls = [
        c for c in tool_calls
        if c.get("name") == tool_name or c.get("name", "").split(":")[-1] == tool_name
    ]
    if not matching_calls:
        return None
    if len(matching_calls) == 1:
        return matching_calls[0]
        
    # Disambiguate if multiple calls exist
    if command_arg:
        for call in matching_calls:
            cmd = call.get("args", {}).get("CommandLine") or ""
            if clean_arg(cmd) == clean_arg(command_arg):
                return call
    if file_arg:
        for call in matching_calls:
            file_val = (
                call.get("args", {}).get("TargetFile") or 
                call.get("args", {}).get("target_file") or 
                call.get("args", {}).get("AbsolutePath") or
                call.get("args", {}).get("absolute_path") or
                ""
            )
            if clean_arg(file_val) == clean_arg(file_arg):
                return call
    return matching_calls[0]

def log_audit_action(tool_call, directive, status, reason):
    log_path = Path(__file__).resolve().parent / "audit_logs.json"
    new_entry = {
        "timestamp": datetime.now().isoformat(),
        "tool": tool_call.get("name"),
        "arguments": tool_call.get("args"),
        "directive": directive,
        "status": status,
        "reason": reason
    }
    logs = []
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            pass
    logs.insert(0, new_entry)
    logs = logs[:100]
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        print(f"⚠️ Uppercase Enforcer: Failed to write audit log: {e}")

def log_blocked_action(tool_call, directive, reason):
    log_audit_action(tool_call, directive, "FAIL", reason)

def call_deepseek(api_key, system_prompt, user_content):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30.0) as response:
        res = json.loads(response.read().decode("utf-8"))
        return res["choices"][0]["message"]["content"]

def main():
    parser = argparse.ArgumentParser(description="Enforces compliance with user's uppercase directives")
    parser.add_argument("--tool", required=True, help="Proposed tool name")
    parser.add_argument("--command", help="CommandLine parameter for run_command")
    parser.add_argument("--file", help="TargetFile parameter for file edits")
    args = parser.parse_args()

    # Load custom directives
    custom_directives_path = Path(__file__).resolve().parent / "custom_directives.json"
    custom_directives = []
    if custom_directives_path.exists():
        try:
            with open(custom_directives_path, "r", encoding="utf-8") as f:
                custom_directives = json.load(f)
        except Exception as e:
            print(f"⚠️ Uppercase Enforcer: Failed to read custom directives: {e}")

    # Load caps lock state
    caps_lock_path = Path(__file__).resolve().parent / "caps_lock_state.json"
    caps_lock_state = {"active": False, "superprompt": ""}
    if caps_lock_path.exists():
        try:
            with open(caps_lock_path, "r", encoding="utf-8") as f:
                caps_lock_state = json.load(f)
        except Exception:
            pass

    transcript_path = find_active_transcript()
    user_prompts = []
    if transcript_path:
        user_prompts = extract_user_prompts(transcript_path)

    # Automatically activate Caps Lock Protection if the latest prompt is in all caps
    if user_prompts:
        latest_prompt = user_prompts[-1]
        if is_caps_lock_prompt(latest_prompt):
            if caps_lock_state.get("superprompt") != latest_prompt:
                caps_lock_state["active"] = True
                caps_lock_state["superprompt"] = latest_prompt
                try:
                    with open(caps_lock_path, "w", encoding="utf-8") as f:
                        json.dump(caps_lock_state, f, indent=2)
                except Exception as e:
                    print(f"⚠️ Uppercase Enforcer: Failed to save caps lock state: {e}")

    # Find the proposed tool call and arguments
    tool_call = None
    if transcript_path:
        tool_call = find_proposed_tool_call(transcript_path, args.tool, args.command, args.file)
    
    if not tool_call:
        tool_call = {
            "name": args.tool,
            "args": {
                "CommandLine": args.command,
                "TargetFile": args.file,
                "AbsolutePath": args.file
            }
        }

    # Check if the Question Mark Rule is active and if the latest user prompt ends in '?'
    question_rule_path = Path(__file__).resolve().parent / "question_rule.json"
    question_rule_active = False
    if question_rule_path.exists():
        try:
            with open(question_rule_path, "r", encoding="utf-8") as f:
                question_rule_active = json.load(f).get("active", False)
        except Exception:
            pass

    if question_rule_active and user_prompts:
        latest_prompt = user_prompts[-1].strip()
        if latest_prompt.endswith("?"):
            print(f"❌ UPPERCASE DIRECTIVE VIOLATION: User prompt ends with '?' and Question Mark rule is active.")
            log_audit_action(tool_call, "Prompt ends in '?'", "FAIL", "FAIL: The user's prompt ends in a question mark, and the question-mark tool block rule is active. The agent must answer without calling any tools.")
            sys.exit(1)

    # Load DeepSeek API key
    if not os.path.exists(DEEPSEEK_KEY_PATH):
        print(f"⚠️ Uppercase Enforcer: DeepSeek API key file not found at {DEEPSEEK_KEY_PATH}. Skipping checks (fail-open).")
        sys.exit(0)

    try:
        with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
    except Exception as e:
        print(f"⚠️ Uppercase Enforcer: Failed to read API key: {e}. Skipping checks (fail-open).")
        sys.exit(0)

    if not api_key:
        print("⚠️ Uppercase Enforcer: Empty API key. Skipping checks (fail-open).")
        sys.exit(0)

    checks_conducted = False

    # 1. Audit Caps Lock Protection if active
    if caps_lock_state.get("active") and caps_lock_state.get("superprompt"):
        superprompt = caps_lock_state["superprompt"]
        print(f"🔍 Uppercase Enforcer: [CAPS LOCK PROTECT ACTIVE] Checking proposed '{args.tool}' compliance...")
        checks_conducted = True
        
        system_prompt = (
            "You are a strict compliance auditor. Your job is to verify whether a proposed agent action violates a specific user directive.\n\n"
            "You MUST check if the proposed tool call violates or ignores the User's directive in any way.\n"
            "If there is any possibility of a violation, or if the action contradicts or is restricted by the directive, reply ONLY with a explanation starting with 'FAIL: '.\n"
            "Otherwise, reply PASS."
        )

        user_content = (
            f"User's Uppercase Directive:\n{superprompt}\n\n"
            f"Proposed Tool Call:\n"
            f"Tool Name: {tool_call.get('name')}\n"
            f"Arguments: {json.dumps(tool_call.get('args'), indent=2)}"
        )

        try:
            response_text = call_deepseek(api_key, system_prompt, user_content)
            clean_resp = response_text.strip()
            if not clean_resp.upper().startswith("PASS"):
                if clean_resp.upper().startswith("FAIL") or "FAIL" in clean_resp.upper():
                    print(f"❌ UPPERCASE DIRECTIVE VIOLATION (CAPS LOCK): {clean_resp}")
                    print(f"Directive violated: \"{superprompt}\"")
                    log_audit_action(tool_call, superprompt, "FAIL", clean_resp)
                    sys.exit(1)
            else:
                log_audit_action(tool_call, superprompt, "PASS", "Action compliant with global Caps Lock protection.")
        except Exception as e:
            print(f"⚠️ Uppercase Enforcer: Verification API call failed for caps lock directive ({e}).")

    # 2. Audit Custom Rules (normally, with pertinency check)
    if custom_directives:
        print(f"🔍 Uppercase Enforcer: Checking custom rule compliance for '{args.tool}'...")
        for prompt in custom_directives:
            checks_conducted = True
            system_prompt = (
                "You are a strict compliance auditor. Your job is to verify whether a proposed agent action violates a specific user directive.\n\n"
                "Step 1: Analyze the User's Uppercase Directive and determine if it is pertinent to the proposed tool call. "
                "A directive is pertinent ONLY if the tool call is performing an action directly related to, governed by, or restricted by that directive.\n"
                "Step 2: If the directive is not pertinent to the proposed tool call, reply with PASS.\n"
                "Step 3: If the directive IS pertinent, check if the proposed tool call violates or ignores it.\n"
                "If there is a violation, reply ONLY with a terse and informative one-sentence explanation starting with 'FAIL: '.\n"
                "If there is no violation, reply with PASS."
            )

            user_content = (
                f"User's Uppercase Directive:\n{prompt}\n\n"
                f"Proposed Tool Call:\n"
                f"Tool Name: {tool_call.get('name')}\n"
                f"Arguments: {json.dumps(tool_call.get('args'), indent=2)}"
            )

            try:
                response_text = call_deepseek(api_key, system_prompt, user_content)
                clean_resp = response_text.strip()
                if clean_resp.upper().startswith("PASS"):
                    log_audit_action(tool_call, prompt, "PASS", "Action compliant with custom rule.")
                    continue
                elif clean_resp.upper().startswith("FAIL") or "FAIL" in clean_resp.upper():
                    print(f"❌ UPPERCASE DIRECTIVE VIOLATION (CUSTOM): {clean_resp}")
                    print(f"Directive violated: \"{prompt}\"")
                    log_audit_action(tool_call, prompt, "FAIL", clean_resp)
                    sys.exit(1)
            except Exception as e:
                print(f"⚠️ Uppercase Enforcer: Verification API call failed for directive ({e}). Skipping this check.")
                continue

    if not checks_conducted:
        log_audit_action(tool_call, "None", "PASS", "No active compliance rules matched. Action allowed by default.")

    sys.exit(0)
