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
                    mtime = subdir.stat().st_mtime
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
        return "NO_TEST"
    try:
        transcript_path = newest_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_path.exists():
            return "NO_TEST"
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                data = json.loads(line.strip())
                if data.get("type") == "USER_INPUT":
                    content = data.get("content", "")
                    if re.search(r'\bNO\s+TEST\b', content):
                        return "NO_TEST"
                    elif re.search(r'\bTEST\b', content):
                        return "TEST"
            except Exception:
                pass
    except Exception:
        pass
    return "NO_TEST"

def toggle_lock_state(new_state, newest_dir):
    if not newest_dir:
        return False
    try:
        transcript_path = newest_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if transcript_path.exists():
            date_str = "2026-06-08T10:00:00Z"
            new_prompt = "TEST" if new_state == "TEST" else "NO TEST"
            new_line = json.dumps({
                "step_index": 999999,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": date_str,
                "content": f"<USER_REQUEST>\n{new_prompt}\n</USER_REQUEST>"
            }) + "\n"
            with open(transcript_path, "a", encoding="utf-8") as f:
                f.write(new_line)
            return True
    except Exception as e:
        print(f"Error toggling state: {e}")
    return False

def apply_enforcer_changes(state):
    action = "pre_run" if state == "TEST" else "pre_write"
    # Call the enforcer script directly to adjust permissions and update settings.json
    cmd = [sys.executable, "/Users/orpington/.gemini/config/plugins/sc-guard-enforcer/test_lock_enforcer.py", "--action", action]
    if action == "pre_run":
        cmd += ["--command", "pytest"]
    subprocess.run(cmd, capture_output=True)

class LockServerHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/toggle":
            self.handle_api_toggle()
        elif self.path == "/api/run-tests":
            self.handle_api_run_tests()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.serve_dashboard()
        elif self.path == "/api/state":
            self.handle_api_state()
        elif self.path == "/api/toggle" or self.path == "/toggle":
            # Support GET for toggle to handle legacy link clicking
            self.handle_api_toggle()
        else:
            self.send_response(404)
            self.end_headers()

    def serve_dashboard(self):
        dashboard_path = Path("/Users/orpington/Documents/economy-documentary-work/scripts/dashboard.html")
        if not dashboard_path.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Dashboard file not found.")
            return

        try:
            with open(dashboard_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Error serving dashboard: {e}".encode('utf-8'))

    def handle_api_state(self):
        newest_dir = get_newest_brain_dir()
        state = get_lock_state(newest_dir)
        response_data = {
            "state": state,
            "readonly": state == "TEST",
            "brain_dir": str(newest_dir) if newest_dir else None
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response_data).encode('utf-8'))

    def handle_api_toggle(self):
        newest_dir = get_newest_brain_dir()
        current_state = get_lock_state(newest_dir)
        new_state = "TEST" if current_state == "NO_TEST" else "NO_TEST"
        
        # Toggle state & apply enforcer permissions + vscode settings changes
        toggle_lock_state(new_state, newest_dir)
        apply_enforcer_changes(new_state)
        
        if self.path.startswith("/api/"):
            # Return API JSON
            response_data = {
                "state": new_state,
                "readonly": new_state == "TEST"
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode('utf-8'))
        else:
            # Legacy link path /toggle: redirect to the homepage dashboard
            self.send_response(302)
            self.send_header('Location', '/')
            self.end_headers()

    def handle_api_run_tests(self):
        # Start response streaming
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        project_root = "/Users/orpington/Documents/economy-documentary-work"
        python_exec = os.path.join(project_root, ".venv/bin/python")
        run_script = os.path.join(project_root, "tests/units/run.py")

        if not os.path.exists(python_exec):
            self.wfile.write(b"Error: Virtual environment python interpreter not found.\n")
            return

        self.wfile.write(b"Spawning isolated test runner process...\n")
        self.wfile.flush()

        try:
            # Spawn the test runner. Note: We inject PYTHONPATH to find modules.
            # We also run it through the lock_enforcer logic.
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
            
            # Read stdout line-by-line and write it directly to the response socket
            for line in iter(process.stdout.readline, ""):
                self.wfile.write(line.encode('utf-8'))
                self.wfile.flush()
                
            process.stdout.close()
            process.wait()
            
            self.wfile.write(f"\n[Test process exited with code {process.returncode}]\n".encode('utf-8'))
            self.wfile.flush()
        except Exception as e:
            self.wfile.write(f"\nException raised during test execution: {e}\n".encode('utf-8'))
            self.wfile.flush()

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
