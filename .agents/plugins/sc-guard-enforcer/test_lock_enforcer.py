#!/usr/bin/env python3
import sys
import os
import argparse
import json
import re
import socket
import subprocess
from pathlib import Path

# Path to search for test processes
TEST_PATTERNS = ["pytest", "tests.units.run", "tests/units/run", "run.py"]

def get_newest_brain_dir():
    try:
        brain_root = Path("/Users/orpington/.gemini/antigravity/brain")
        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        if conv_id and (brain_root / conv_id).exists():
            return brain_root / conv_id
        if not brain_root.exists():
            return None
        newest_dir = None
        newest_mtime = 0
        for subdir in brain_root.iterdir():
            if subdir.is_dir() and not subdir.name.startswith("."):
                try:
                    state_file = subdir / ".lock_state"
                    mtime = state_file.stat().st_mtime if state_file.exists() else subdir.stat().st_mtime
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        newest_dir = subdir
                except Exception:
                    pass
        return newest_dir
    except Exception:
        return None

def get_lock_state(newest_dir):
    if not newest_dir:
        return "WRITE_ALLOWED"
    
    # Try reading the state file
    state_file = newest_dir / ".lock_state"
    if state_file.exists():
        try:
            val = state_file.read_text(encoding="utf-8").strip()
            if val in ["TESTS_ONLY", "ANALYZE_ONLY", "WRITE_ALLOWED", "TEST", "NO_TEST"]:
                # Map old values for backward compatibility
                if val == "TEST":
                    return "TESTS_ONLY"
                if val == "NO_TEST":
                    return "WRITE_ALLOWED"
                return val
        except Exception:
            pass

    # Fallback to transcript
    try:
        transcript_path = newest_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_path.exists():
            return "WRITE_ALLOWED"
            
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        for line in reversed(lines):
            try:
                data = json.loads(line.strip())
                if data.get("type") == "USER_INPUT":
                    content = data.get("content", "")
                    if re.search(r'\bNO\s+TEST\b', content):
                        return "WRITE_ALLOWED"
                    elif re.search(r'\bTEST\b', content):
                        return "TESTS_ONLY"
            except Exception:
                pass
    except Exception:
        pass
    return "WRITE_ALLOWED"

def update_vscode_settings(state):
    try:
        project_root = Path("/Users/orpington/Documents/economy-documentary-work")
        vscode_dir = project_root / ".vscode"
        if not vscode_dir.exists():
            vscode_dir.mkdir(parents=True, exist_ok=True)
            
        settings_path = vscode_dir / "settings.json"
        
        settings = {}
        if settings_path.exists():
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            except Exception:
                pass
                
        blood_red_customizations = {
            "editor.background": "#1b0000",
            "sideBar.background": "#240000",
            "sideBarSectionHeader.background": "#2e0000",
            "activityBar.background": "#330000",
            "activityBar.activeBackground": "#4a0000",
            "activityBarBadge.background": "#ff4d4f",
            "statusBar.background": "#4a0000",
            "statusBar.foreground": "#ffffff",
            "titleBar.activeBackground": "#240000",
            "tab.activeBackground": "#3a0000",
            "tab.inactiveBackground": "#150000"
        }
        
        amber_customizations = {
            "editor.background": "#1f1402",
            "sideBar.background": "#291a03",
            "sideBarSectionHeader.background": "#332104",
            "activityBar.background": "#3d2705",
            "activityBar.activeBackground": "#523507",
            "activityBarBadge.background": "#f59e0b",
            "statusBar.background": "#523507",
            "statusBar.foreground": "#ffffff",
            "titleBar.activeBackground": "#291a03",
            "tab.activeBackground": "#3d2705",
            "tab.inactiveBackground": "#170f01"
        }
        
        if state in ["TESTS_ONLY", "TEST"]:
            settings["workbench.colorCustomizations"] = blood_red_customizations
        elif state == "ANALYZE_ONLY":
            settings["workbench.colorCustomizations"] = amber_customizations
        else:
            if "workbench.colorCustomizations" in settings:
                del settings["workbench.colorCustomizations"]
                
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
            
    except Exception as e:
        print(f"⚠️ Failed to update VS Code settings: {e}")

def update_html_artifact(state, newest_dir):
    if not newest_dir:
        return
    try:
        md_path = newest_dir / "lock_status.md"
        
        # Generate PNG button images dynamically using PIL
        try:
            from PIL import Image, ImageDraw, ImageFont
            
            def create_button_png(text, filename, start_color, end_color):
                width, height = 280, 36
                img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                
                # Draw vertical gradient
                for y in range(height):
                    r = int(start_color[0] + (end_color[0] - start_color[0]) * y / height)
                    g = int(start_color[1] + (end_color[1] - start_color[1]) * y / height)
                    b = int(start_color[2] + (end_color[2] - start_color[2]) * y / height)
                    draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
                    
                # Rounded corners mask
                mask = Image.new("L", (width, height), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle([(0, 0), (width - 1, height - 1)], radius=6, fill=255)
                
                # Apply mask to gradient
                gradient_img = Image.new("RGBA", (width, height))
                gradient_img.paste(img, (0, 0), mask=mask)
                
                # Draw text
                draw_text = ImageDraw.Draw(gradient_img)
                font = None
                font_names = [
                    "/System/Library/Fonts/SFNS.ttf",
                    "/System/Library/Fonts/Helvetica.ttc",
                    "/System/Library/Fonts/LucidaGrande.ttc",
                    "Arial.ttf"
                ]
                for font_path in font_names:
                    if os.path.exists(font_path):
                        try:
                            font = ImageFont.truetype(font_path, 12)
                            break
                        except Exception:
                            continue
                if not font:
                    font = ImageFont.load_default()
                    
                try:
                    bbox = draw_text.textbbox((0, 0), text, font=font)
                    text_w = bbox[2] - bbox[0]
                    text_h = bbox[3] - bbox[1]
                except AttributeError:
                    text_w, text_h = draw_text.textsize(text, font=font)
                    
                x = (width - text_w) / 2
                y = (height - text_h) / 2 - 1
                
                draw_text.text((x, y), text, fill=(255, 255, 255, 255), font=font)
                gradient_img.save(filename, "PNG")

            create_button_png("RUN TEST SUITE", newest_dir / "run_tests_button.png", (37, 99, 235), (29, 78, 216))
            create_button_png("ALLOW CODE READ (ANALYZE)", newest_dir / "allow_read_button.png", (245, 158, 11), (217, 119, 6))
            create_button_png("ALLOW FULL WRITES (NO TEST)", newest_dir / "allow_write_button.png", (16, 185, 129), (4, 120, 87))
            create_button_png("LOCK & RUN TESTS", newest_dir / "lock_tests_button.png", (239, 68, 68), (185, 28, 28))
            create_button_png("LOCK FOR ANALYZE (READ-ONLY)", newest_dir / "lock_analyze_button.png", (220, 38, 38), (153, 27, 27))
            
        except Exception as e:
            print(f"⚠️ Failed to generate button PNGs: {e}")

        tests_only_md = f"""# 🛡️ Lock Status Control

> [!CAUTION]
> ### 🔒 TESTS ONLY MODE (READ-ONLY)
> The codebase is locked in read-only mode.
> The agent is **BLOCKED** from reading codebase files and **BLOCKED** from writing files.
> The agent can **ONLY** read test output files in the `test_outputs/` directory.
> Running pytest integration tests is **ENABLED**.
>
> [![Run Test Suite](http://localhost:19245/images/run_tests_button.png)](http://localhost:19245/run-tests)
>
> To let the agent inspect codebase files for debugging:
> [![Allow Code Read (Analyze)](http://localhost:19245/images/allow_read_button.png)](http://localhost:19245/change-state?state=ANALYZE_ONLY)
>
> To allow full writes:
> [![Allow Full Writes (No Test)](http://localhost:19245/images/allow_write_button.png)](http://localhost:19245/change-state?state=WRITE_ALLOWED)
"""

        analyze_only_md = f"""# 🛡️ Lock Status Control

> [!WARNING]
> ### 📖 ANALYZE CODE MODE (READ-ONLY)
> The codebase is locked in read-only mode to prevent a frenzy of writing.
> The agent is **ALLOWED** to read codebase files for analysis, but **BLOCKED** from writing.
> Running pytest integration tests is **DISABLED**.
>
> To run tests again:
> [![Lock & Run Tests](http://localhost:19245/images/lock_tests_button.png)](http://localhost:19245/change-state?state=TESTS_ONLY)
>
> To allow full writes:
> [![Allow Full Writes (No Test)](http://localhost:19245/images/allow_write_button.png)](http://localhost:19245/change-state?state=WRITE_ALLOWED)
"""

        write_allowed_md = f"""# 🛡️ Lock Status Control

> [!TIP]
> ### 🔓 WRITE MODE (FULL ACCESS)
> Full write-access is enabled. The agent can modify and create codebase files.
> Running pytest integration tests is **DISABLED**.
>
> To lock the codebase and run tests:
> [![Lock & Run Tests](http://localhost:19245/images/lock_tests_button.png)](http://localhost:19245/change-state?state=TESTS_ONLY)
>
> To lock codebase in read-only mode for analysis:
> [![Lock for Analyze (Read-Only)](http://localhost:19245/images/lock_analyze_button.png)](http://localhost:19245/change-state?state=ANALYZE_ONLY)
"""

        if state in ["TESTS_ONLY", "TEST"]:
            target_content = tests_only_md
        elif state == "ANALYZE_ONLY":
            target_content = analyze_only_md
        else:
            target_content = write_allowed_md
        
        # Append test report
        try:
            test_outputs = newest_dir / "test_outputs"
            if test_outputs.exists():
                bdd_dir = test_outputs / "bdd_verdicts"
                bdd_results = []
                if bdd_dir.exists():
                    for f_item in sorted(bdd_dir.iterdir()):
                        if f_item.name.endswith(".json"):
                            try:
                                with open(f_item, "r", encoding="utf-8") as jf:
                                    bdd_results.append(json.load(jf))
                            except Exception:
                                pass
                
                pytest_log = test_outputs / "pytest_output.log"
                passed_count = 0
                failed_count = 0
                skipped_count = 0
                if pytest_log.exists():
                    with open(pytest_log, "r", encoding="utf-8") as lf:
                        log_content = lf.read()
                    passed_count = len(re.findall(r"SUMMARY: TEST CASE '([^']+)' COMPLETED SUCCESSFULLY AND PASSED", log_content))
                    failed_count = len(re.findall(r"SUMMARY: TEST CASE '([^']+)' FAILED", log_content))
                    skipped_count = len(re.findall(r"SUMMARY: TEST CASE '([^']+)' WAS SKIPPED", log_content))
                
                md = ["\n## 📊 Latest Test Run Report"]
                total = passed_count + failed_count + skipped_count
                if total > 0:
                    md.append(f"\n### 📈 Summary: **{passed_count} Passed**, **{failed_count} Failed**, **{skipped_count} Skipped** (Total: {total} tests)\n")
                else:
                    md.append("\nNo recent test run summary found.\n")
                
                if bdd_results:
                    md.append("\n### 🏷️ BDD Integration Tests Verdicts\n")
                    md.append("| BDD Test Case | Verdict | Confidence | Key Issues / Reasoning |")
                    md.append("| :--- | :---: | :---: | :--- |")
                    for res in bdd_results:
                        name = res.get("test_name", "Unknown")
                        verdict = res.get("verdict", "Unknown").upper()
                        conf = f"{int(res.get('confidence', 0) * 100)}%"
                        reasoning = res.get("reasoning", "")
                        reasoning = reasoning.replace("\n", " ").replace("|", "\\|")
                        v_emoji = "✅ PASS" if verdict == "PASS" else "❌ FAIL"
                        md.append(f"| `{name}` | {v_emoji} | {conf} | {reasoning} |")
                
                target_content += "\n" + "\n".join(md)
        except Exception as e:
            pass

        # Avoid writing if it is already identical
        if md_path.exists():
            with open(md_path, "r", encoding="utf-8") as f:
                current_content = f.read()
            if current_content == target_content:
                return
                
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(target_content)
    except Exception as e:
        print(f"⚠️ Failed to update Markdown artifact: {e}")

def is_server_running():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", 19245))
            return True
    except Exception:
        return False

def start_server_background():
    try:
        script_path = "/Users/orpington/Documents/economy-documentary-work/scripts/lock_server.py"
        subprocess.Popen([sys.executable, script_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("🚀 [Lock Enforcer] Spawning background lock server on port 19245...")
    except Exception as e:
        print(f"⚠️ Failed to spawn lock server: {e}")

def make_codebase_readonly():
    project_root = "/Users/orpington/Documents/economy-documentary-work"
    changed = False
    for dir_name in ["server", "tests"]:
        dir_path = os.path.join(project_root, dir_name)
        if os.path.exists(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f.endswith(".py"):
                        path = os.path.join(root, f)
                        try:
                            stat = os.stat(path)
                            if (stat.st_mode & 0o200) != 0:
                                os.chmod(path, 0o444)  # Read-only
                                changed = True
                        except Exception:
                            pass
    if changed:
        print("🔒 [Lock Enforcer] Codebase files set to read-only.")

def restore_write_permissions():
    project_root = "/Users/orpington/Documents/economy-documentary-work"
    changed = False
    for dir_name in ["server", "tests"]:
        dir_path = os.path.join(project_root, dir_name)
        if os.path.exists(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f.endswith(".py"):
                        path = os.path.join(root, f)
                        try:
                            stat = os.stat(path)
                            if (stat.st_mode & 0o200) == 0:
                                os.chmod(path, 0o644)  # Read-write
                                changed = True
                        except Exception:
                            pass
    if changed:
        print("🔓 [Lock Enforcer] Codebase files set to read-write.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True, choices=["pre_write", "pre_run", "pre_read"])
    parser.add_argument("--file", help="Target file/path being read/written")
    parser.add_argument("--command", help="Command line being executed")
    args = parser.parse_args()

    # 1. Start background lock server if not active
    if not is_server_running():
        start_server_background()

    newest_dir = get_newest_brain_dir()
    state = get_lock_state(newest_dir)

    # 2. Sync codebase file permissions based on state
    if state in ["TESTS_ONLY", "ANALYZE_ONLY", "TEST"]:
        make_codebase_readonly()
    else:
        restore_write_permissions()
    
    # 3. Update editor color customizations in .vscode/settings.json
    update_vscode_settings(state)
    
    # 4. Update Markdown artifact view based on state
    update_html_artifact(state, newest_dir)

    if args.action == "pre_run":
        print("❌ ERROR [Lock Enforcer]: Running terminal commands (run_command) is completely BLOCKED.")
        print("All test executions and commands must be run manually by the user.")
        sys.exit(1)

    elif args.action == "pre_write":
        target_file = args.file or ""
        abs_target = os.path.abspath(target_file)
        
        # Block editing lock/state config files entirely
        filename = os.path.basename(abs_target)
        if filename in [".lock_state", "test_lock_enforcer.py", "hooks.json", "sc_guard_enforcer.py", "plugin.json", "lock_server.py"]:
            print(f"❌ ERROR [Lock Enforcer]: Modifying lock configuration or state files ({filename}) is strictly PROHIBITED.")
            sys.exit(1)
            
        # Allow writes to brain/artifact files (like lock_status.md, task.md, etc.) in all modes
        brain_root = os.path.abspath("/Users/orpington/.gemini/antigravity/brain")
        if abs_target.startswith(brain_root):
            sys.exit(0)
            
        if state in ["TESTS_ONLY", "ANALYZE_ONLY", "TEST"]:
            print(f"❌ ERROR [Lock Enforcer]: Write rejected!")
            print(f"The codebase is locked in read-only mode because '{state}' mode is active.")
            sys.exit(1)
            
        sys.exit(0)

    elif args.action == "pre_read":
        target_file = args.file or ""
        abs_target = os.path.abspath(target_file)
        
        # Always allow reading files inside test_outputs
        brain_root = os.path.abspath("/Users/orpington/.gemini/antigravity/brain")
        newest_dir = get_newest_brain_dir()
        test_outputs_path = newest_dir / "test_outputs" if newest_dir else None
        
        if test_outputs_path and abs_target.startswith(os.path.abspath(test_outputs_path)):
            sys.exit(0)
            
        # Allow reading lock_status.md or other files in the brain directory
        if abs_target.startswith(brain_root):
            sys.exit(0)
            
        project_root = "/Users/orpington/Documents/economy-documentary-work"
        
        # Check if target contains or is inside codebase (server/ or tests/ or scripts/)
        is_codebase = False
        for dir_name in ["server", "tests", "scripts"]:
            dir_path = os.path.abspath(os.path.join(project_root, dir_name))
            if abs_target.startswith(dir_path) or dir_path.startswith(abs_target):
                is_codebase = True
                break
                
        if state in ["TESTS_ONLY", "TEST"] and is_codebase:
            print("❌ ERROR [Lock Enforcer]: Reading codebase files is BLOCKED under TESTS_ONLY mode.")
            print("You can only read test output/verdict files inside the brain directory under test_outputs/.")
            sys.exit(1)
            
        sys.exit(0)

if __name__ == "__main__":
    main()
