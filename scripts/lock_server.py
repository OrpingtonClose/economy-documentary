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
    # In pre_run mode, we need a mock command to avoid check errors
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

    def do_GET(self):
        if self.path.startswith("/toggle"):
            newest_dir = get_newest_brain_dir()
            current_state = get_lock_state(newest_dir)
            new_state = "TEST" if current_state == "NO_TEST" else "NO_TEST"
            
            # Toggle state & apply enforcer permissions + vscode settings changes
            toggle_lock_state(new_state, newest_dir)
            apply_enforcer_changes(new_state)
            
            # Send HTML page that auto-closes the tab
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            
            msg = "Codebase Locked (TEST Mode Active)" if new_state == "TEST" else "Codebase Unlocked (NO TEST Mode Active)"
            bg = "#240000" if new_state == "TEST" else "#061f0d"
            text_color = "#ff4d4f" if new_state == "TEST" else "#2ecc71"
            
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
                    }}, 1200);
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
            return
            
        self.send_response(404)
        self.end_headers()

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
