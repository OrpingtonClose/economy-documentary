#!/usr/bin/env python3
import http.server
import socketserver
import json
import os
import re
import sys
import subprocess
from pathlib import Path

PORT = 19245

def get_newest_brain_dir():
    try:
        brain_root = Path("/Users/orpington/.gemini/antigravity/brain")
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

class LockServerHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/change-state"):
            self.handle_change_state()
        elif self.path.startswith("/toggle"):  # Kept for backward compatibility
            self.handle_toggle()
        elif self.path.startswith("/run-tests"):
            self.handle_run_tests()
        elif self.path.startswith("/images/"):
            self.handle_image()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def handle_image(self):
        filename = os.path.basename(self.path)
        newest_dir = get_newest_brain_dir()
        if not newest_dir:
            self.send_response(404)
            self.end_headers()
            return
            
        file_path = newest_dir / filename
        if file_path.exists() and filename.endswith(".png"):
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def handle_toggle(self):
        newest_dir = get_newest_brain_dir()
        current_state = get_lock_state(newest_dir)
        new_state = "TESTS_ONLY" if current_state == "WRITE_ALLOWED" else "WRITE_ALLOWED"
        self.perform_state_change(new_state, newest_dir)

    def handle_change_state(self):
        from urllib.parse import urlparse, parse_qs
        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)
        new_state = params.get("state", ["WRITE_ALLOWED"])[0]
        newest_dir = get_newest_brain_dir()
        self.perform_state_change(new_state, newest_dir)

    def perform_state_change(self, new_state, newest_dir):
        if newest_dir:
            state_file = newest_dir / ".lock_state"
            state_file.write_text(new_state, encoding="utf-8")
            
            # Write simulated USER_INPUT to transcript so we log state transitions
            transcript_path = newest_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if transcript_path.exists():
                date_str = "2026-06-08T10:00:00Z"
                new_prompt = "TEST" if new_state == "TESTS_ONLY" else "NO TEST"
                new_line = json.dumps({
                    "step_index": 999999,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": date_str,
                    "content": f"<USER_REQUEST>\n{new_prompt}\n</USER_REQUEST>"
                }) + "\n"
                try:
                    with open(transcript_path, "a", encoding="utf-8") as f:
                        f.write(new_line)
                except Exception:
                    pass

            # Sync enforcer permission changes and VS Code colors
            cmd = [
                sys.executable,
                "/Users/orpington/.gemini/config/plugins/sc-guard-enforcer/test_lock_enforcer.py",
                "--action", "pre_write",
                "--file", str(newest_dir / "lock_status.md")
            ]
            subprocess.run(cmd, capture_output=True)
            
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        
        msg = f"State Changed to: {new_state}"
        if new_state in ["TESTS_ONLY", "TEST"]:
            msg = "Locked in TESTS ONLY mode"
            bg = "#240000"
            text_color = "#ff4d4f"
        elif new_state == "ANALYZE_ONLY":
            msg = "Locked in ANALYZE CODE mode"
            bg = "#291a03"
            text_color = "#f59e0b"
        else:
            msg = "Unlocked in WRITE mode"
            bg = "#061f0d"
            text_color = "#2ecc71"
            
        html = f"""<!DOCTYPE html>
        <html>
        <head>
            <title>{msg}</title>
            <style>
                body {{
                    background-color: {bg};
                    color: #ffffff;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    height: 90vh;
                    margin: 0;
                    text-align: center;
                }}
                .card {{
                    padding: 30px;
                    border-radius: 12px;
                    background: rgba(0, 0, 0, 0.3);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    max-width: 400px;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
                }}
                h1 {{ color: {text_color}; margin-top: 0; }}
                p {{ color: #cccccc; font-size: 0.95rem; line-height: 1.4; }}
            </style>
            <script>
                setTimeout(() => {{
                    window.close();
                }}, 1000);
            </script>
        </head>
        <body>
            <div class="card">
                <h1>{msg}</h1>
                <p>The lock state has been updated successfully.</p>
                <p><i>This tab will close automatically...</i></p>
            </div>
        </body>
        </html>"""
        self.wfile.write(html.encode('utf-8'))

    def handle_run_tests(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()

        project_root = "/Users/orpington/Documents/economy-documentary-work"
        python_exec = os.path.join(project_root, ".venv/bin/python")
        run_script = os.path.join(project_root, "tests/units/run.py")

        if not os.path.exists(python_exec):
            self.wfile.write(b"Error: Virtual environment python interpreter not found.\n")
            return

        newest_dir = get_newest_brain_dir()
        if not newest_dir:
            self.wfile.write(b"Error: Newest brain directory not found.\n")
            return

        # Create test_outputs directory
        test_outputs_dir = newest_dir / "test_outputs"
        test_outputs_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = test_outputs_dir / "pytest_output.log"
        
        try:
            log_file = open(log_file_path, "w", encoding="utf-8")
        except Exception:
            log_file = None

        self.wfile.write(b"Executing test suite via pytest...\n\n")
        if log_file:
            log_file.write("Executing test suite via pytest...\n\n")
            
        try:
            env = dict(os.environ, PYTHONPATH=f"{project_root}/server:{project_root}/server/capabilities")
            process = subprocess.Popen(
                [python_exec, run_script],
                cwd=project_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            for line in iter(process.stdout.readline, ""):
                self.wfile.write(line.encode('utf-8'))
                self.wfile.flush()
                if log_file:
                    log_file.write(line)
                    log_file.flush()
            process.stdout.close()
            process.wait()
            
            exit_msg = f"\n[Test process exited with code {process.returncode}]\n"
            self.wfile.write(exit_msg.encode('utf-8'))
            if log_file:
                log_file.write(exit_msg)
                log_file.close()
                
            # Copy recently updated bdd_verdicts JSON files
            import tempfile
            import shutil
            from datetime import datetime, timedelta
            sys_temp = tempfile.gettempdir()
            now = datetime.now()
            copied_count = 0
            for root, dirs, files in os.walk(sys_temp):
                if "bdd_verdicts" in root:
                    for f in files:
                        if f.endswith(".json"):
                            src_path = os.path.join(root, f)
                            try:
                                mtime = datetime.fromtimestamp(os.path.getmtime(src_path))
                                # Copy files modified in the last 15 minutes
                                if now - mtime < timedelta(minutes=15):
                                    dest_dir = test_outputs_dir / "bdd_verdicts"
                                    dest_dir.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(src_path, dest_dir / f)
                                    copied_count += 1
                            except Exception:
                                pass
            if copied_count > 0:
                print(f"✅ Copied {copied_count} BDD verdict files to {test_outputs_dir / 'bdd_verdicts'}")
                
        except Exception as e:
            self.wfile.write(f"\nException: {e}\n".encode('utf-8'))
            if log_file:
                log_file.write(f"\nException: {e}\n")
                log_file.close()

def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), LockServerHandler) as httpd:
        print(f"Lock server running on port {PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
