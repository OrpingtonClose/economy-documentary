#!/usr/bin/env python3
import sys
import os
import json
import tempfile
from pathlib import Path
import subprocess

plugin_path = Path("/Users/orpington/Documents/economy-documentary-work/.agents/plugins/uppercase-enforcer/uppercase_enforcer.py")

def create_mock_transcript(user_prompts, proposed_tool, proposed_args):
    temp_dir = tempfile.mkdtemp()
    conv_dir = Path(temp_dir) / "mock-conversation-id"
    log_dir = conv_dir / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"
    
    with open(transcript_file, "w", encoding="utf-8") as f:
        for i, prompt in enumerate(user_prompts):
            step = {
                "step_index": i * 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nLocal time\n</ADDITIONAL_METADATA>"
            }
            f.write(json.dumps(step) + "\n")
            
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

def run_test_case(test_name, user_prompts, proposed_tool, proposed_args, cli_args, ignored_data=None, edited_data=None, expected_exit_code=0):
    print(f"--- Running Test: {test_name} ---")
    temp_dir, transcript_file = create_mock_transcript(user_prompts, proposed_tool, proposed_args)
    
    script_content = plugin_path.read_text(encoding="utf-8")
    patched_content = script_content.replace(
        'BRAIN_DIR = Path("/Users/orpington/.gemini/antigravity/brain")',
        f'BRAIN_DIR = Path("{temp_dir}")'
    )
    
    # Write the script to the temp directory
    temp_script_path = Path(temp_dir) / "test_enforcer.py"
    temp_script_path.write_text(patched_content, encoding="utf-8")
    
    # Write ignored and edited json if specified
    if ignored_data is not None:
        with open(Path(temp_dir) / "ignored_directives.json", "w") as f:
            json.dump(ignored_data, f)
    if edited_data is not None:
        with open(Path(temp_dir) / "edited_directives.json", "w") as f:
            json.dump(edited_data, f)
            
    cmd = ["python3", str(temp_script_path)] + cli_args
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    print("STDOUT:")
    print(result.stdout)
    print("STDERR:")
    print(result.stderr)
    print(f"Exit Code: {result.returncode} (Expected: {expected_exit_code})")
    
    # Clean up
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
    
    # Test 1: Baseline violation (no ignore/edit)
    success &= run_test_case(
        "Baseline Violation (MUST NOT WRITE TO README)",
        ["MUST NOT WRITE TO README"],
        "write_to_file",
        {"TargetFile": "README.md", "CodeContent": "new content"},
        ["--tool", "write_to_file", "--file", "README.md"],
        expected_exit_code=1
    )
    
    # Test 2: Ignored directive (should bypass)
    success &= run_test_case(
        "Ignored Directive Bypass",
        ["MUST NOT WRITE TO README"],
        "write_to_file",
        {"TargetFile": "README.md", "CodeContent": "new content"},
        ["--tool", "write_to_file", "--file", "README.md"],
        ignored_data=["MUST NOT WRITE TO README"],
        expected_exit_code=0
    )
    
    # Test 3: Edited directive (should block updated rule but pass old rule target if different)
    # The original is MUST NOT WRITE TO README, edited to MUST NOT WRITE TO TODO.md
    success &= run_test_case(
        "Edited Directive Blocks New Target",
        ["MUST NOT WRITE TO README"],
        "write_to_file",
        {"TargetFile": "TODO.md", "CodeContent": "new content"},
        ["--tool", "write_to_file", "--file", "TODO.md"],
        edited_data={"MUST NOT WRITE TO README": "MUST NOT WRITE TO TODO.md"},
        expected_exit_code=1
    )
    
    success &= run_test_case(
        "Edited Directive Passes Old Target",
        ["MUST NOT WRITE TO README"],
        "write_to_file",
        {"TargetFile": "README.md", "CodeContent": "new content"},
        ["--tool", "write_to_file", "--file", "README.md"],
        edited_data={"MUST NOT WRITE TO README": "MUST NOT WRITE TO TODO.md"},
        expected_exit_code=0
    )

    if success:
        print("🎉 ALL OVERRIDE TESTS PASSED!")
        sys.exit(0)
    else:
        print("❌ SOME OVERRIDE TESTS FAILED!")
        sys.exit(1)

if __name__ == "__main__":
    main()
