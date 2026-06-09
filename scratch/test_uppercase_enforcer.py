#!/usr/bin/env python3
import sys
import os
import json
import tempfile
from pathlib import Path
import subprocess

plugin_path = Path("/Users/orpington/Documents/economy-documentary-work/.agents/plugins/uppercase-enforcer/uppercase_enforcer.py")

def create_mock_transcript(user_prompts, proposed_tool, proposed_args):
    """Creates a temporary transcript.jsonl file with specified content."""
    temp_dir = tempfile.mkdtemp()
    # Create the conversation sub-directory to match production layout
    conv_dir = Path(temp_dir) / "mock-conversation-id"
    log_dir = conv_dir / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"
    
    with open(transcript_file, "w", encoding="utf-8") as f:
        # Write user prompts
        for i, prompt in enumerate(user_prompts):
            step = {
                "step_index": i * 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nLocal time\n</ADDITIONAL_METADATA>"
            }
            f.write(json.dumps(step) + "\n")
            
        # Write planner response with proposed tool call
        planner_step = {
            "step_index": len(user_prompts) * 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": proposed_tool,
                    "args": proposed_args
                }
            ]
        }
        f.write(json.dumps(planner_step) + "\n")
        
    return temp_dir, transcript_file

def run_test_case(test_name, user_prompts, proposed_tool, proposed_args, cli_args, expected_exit_code):
    print(f"--- Running Test: {test_name} ---")
    temp_dir, transcript_file = create_mock_transcript(user_prompts, proposed_tool, proposed_args)
    
    script_content = plugin_path.read_text(encoding="utf-8")
    patched_content = script_content.replace(
        'BRAIN_DIR = Path("/Users/orpington/.gemini/antigravity/brain")',
        f'BRAIN_DIR = Path("{temp_dir}")'
    )
    
    temp_script = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    temp_script.write(patched_content.encode("utf-8"))
    temp_script.close()
    
    cmd = ["python3", temp_script.name] + cli_args
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    print("STDOUT:")
    print(result.stdout)
    print("STDERR:")
    print(result.stderr)
    print(f"Exit Code: {result.returncode} (Expected: {expected_exit_code})")
    
    # Clean up
    os.remove(temp_script.name)
    import shutil
    shutil.rmtree(temp_dir)
    
    if result.returncode == expected_exit_code:
        print(f"✅ {test_name}: SUCCESS\n")
        return True
    else:
        print(f"❌ {test_name}: FAILED\n")
        return False

def main():
    success = True
    
    # Test 1: Bypass when no uppercase directives are in prompt
    success &= run_test_case(
        "Bypass Test (No Uppercase Directives)",
        ["hello world", "please implement feature x"],
        "run_command",
        {"CommandLine": "ls"},
        ["--tool", "run_command", "--command", "ls"],
        expected_exit_code=0
    )
    
    # Test 2: Uppercase directive exists but tool complies (compliant case)
    success &= run_test_case(
        "Compliance Test (Compliant Action)",
        ["Do NOT write to README.md file"],
        "run_command",
        {"CommandLine": "ls"},
        ["--tool", "run_command", "--command", "ls"],
        expected_exit_code=0
    )
    
    # Test 3: Uppercase directive exists and tool violates (violation case)
    success &= run_test_case(
        "Violation Test (Disobeying Action)",
        ["Do NOT write to README.md file"],
        "write_to_file",
        {"TargetFile": "README.md", "CodeContent": "new content"},
        ["--tool", "write_to_file", "--file", "README.md"],
        expected_exit_code=1
    )

    if success:
        print("🎉 ALL TESTS PASSED!")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED!")
        sys.exit(1)

if __name__ == "__main__":
    main()
