#!/usr/bin/env python3
import os
import sys
import json
import re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

PLUGIN_DIR = Path("/Users/orpington/Documents/economy-documentary-work/.agents/plugins/uppercase-enforcer")
BRAIN_DIR = Path("/Users/orpington/.gemini/antigravity/brain")
PORT = 5800

def load_caps_lock_state():
    state_path = PLUGIN_DIR / "caps_lock_state.json"
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"active": False, "superprompt": ""}

def load_question_rule_state():
    state_path = PLUGIN_DIR / "question_rule.json"
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"active": False}

def save_question_rule_state(state):
    state_path = PLUGIN_DIR / "question_rule.json"
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save question rule state: {e}")

def save_caps_lock_state(state):
    state_path = PLUGIN_DIR / "caps_lock_state.json"
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save caps lock state: {e}")

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
    words = re.findall(r"\b[a-zA-Z]{3,}\b", prompt)
    if not words:
        return False
    return all(w.isupper() for w in words)


def find_active_test_results():
    if not BRAIN_DIR.exists():
        return None
    newest_results = None
    newest_mtime = 0
    for subdir in BRAIN_DIR.iterdir():
        if subdir.is_dir():
            results_file = subdir / "test_results.md"
            if results_file.exists():
                try:
                    mtime = results_file.stat().st_mtime
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        newest_results = results_file
                except Exception:
                    pass
    return newest_results

def load_test_results():
    path = find_active_test_results()
    if not path or not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        
        status = "unknown"
        status_match = re.search(r"\*\*Status\*\*:\s*([^\n]+)", content)
        if status_match:
            status = status_match.group(1).strip()
            # Clean up markdown formatting from status
            status = re.sub(r"[\*\`\_]", "", status)
            
        stats = {"passed": 0, "failed": 0, "skipped": 0, "pending": 0}
        stats_match = re.findall(r"\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", content)
        if stats_match:
            stats["passed"] = int(stats_match[0][0])
            stats["failed"] = int(stats_match[0][1])
            stats["skipped"] = int(stats_match[0][2])
            stats["pending"] = int(stats_match[0][3])
        else:
            passed_match = re.search(r"\*\*\s*Passed\s*\*\*:\s*(\d+)", content, re.IGNORECASE)
            failed_match = re.search(r"\*\*\s*Failed\s*\*\*:\s*(\d+)", content, re.IGNORECASE)
            skipped_match = re.search(r"\*\*\s*Skipped\s*\*\*:\s*(\d+)", content, re.IGNORECASE)
            if passed_match:
                stats["passed"] = int(passed_match.group(1))
            if failed_match:
                stats["failed"] = int(failed_match.group(1))
            if skipped_match:
                stats["skipped"] = int(skipped_match.group(1))
                
        running_test = ""
        running_match = re.search(r"\*\*\s*Test Name\s*\*\*:\s*`([^`]+)`", content)
        if running_match:
            running_test = running_match.group(1)
            
        tests = []
        table_rows = re.findall(r"\|\s*`([^`]+)`\s*\|\s*([^\s\|]+(?:\s+[^\s\|]+)?)\s*\|\s*([\d\.]+)s\s*\|", content)
        for name, outcome, duration in table_rows:
            tests.append({
                "name": name,
                "status": re.sub(r"[\*\`\_]", "", outcome).strip(),
                "duration": float(duration)
            })
            
        list_items = re.findall(r"\*\s*`([^`]+)`:\s*([^\s\(]+(?:\s+[^\s\(]+)?)\s*\(([^\s]+)s\)", content)
        for name, outcome, duration in list_items:
            tests.append({
                "name": name,
                "status": re.sub(r"[\*\`\_]", "", outcome).strip(),
                "duration": float(duration)
            })

        audio_measured = 0
        video_delivered = 0
        phase = "idle"
        
        audio_match = re.search(r"\*\*\s*Audio Track\s*\*\*:\s*`\[?([^\]]*)\]?`\s*\((\d+)/", content, re.IGNORECASE)
        if audio_match:
            audio_measured = int(audio_match.group(2))
        video_match = re.search(r"\*\*\s*Video Track\s*\*\*:\s*`\[?([^\]]*)\]?`\s*\((\d+)/", content, re.IGNORECASE)
        if video_match:
            video_delivered = int(video_match.group(2))
            
        phase_match = re.search(r"\*\*\s*Phase\s*\*\*:\s*`([^`]+)`", content, re.IGNORECASE)
        if phase_match:
            phase = phase_match.group(1)

        vms = []
        vm_matches = re.findall(r"\*\s*VM\s+([^\s]+)\s*\|\s*Role:\s*([^\s]+)\s*\|\s*Status:\s*([^\s]+)", content)
        for vm_id, role, vm_status in vm_matches:
            vms.append({
                "id": vm_id,
                "role": role,
                "status": vm_status
            })

        return {
            "status": status,
            "stats": stats,
            "running_test": running_test,
            "tests": tests,
            "audio_measured": audio_measured,
            "video_delivered": video_delivered,
            "phase": phase,
            "vms": vms
        }
    except Exception as e:
        print(f"Error parsing test results: {e}")
        return None


class EnforcerHandler(BaseHTTPRequestHandler):
    def send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def send_file(self, file_path, content_type):
        if not file_path.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"File not found")
            return
        
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(file_path, "rb") as f:
            self.wfile.write(f.read())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == "/":
            self.send_file(PLUGIN_DIR / "index.html", "text/html")
        elif path == "/index.css":
            self.send_file(PLUGIN_DIR / "index.css", "text/css")
        elif path == "/index.js":
            self.send_file(PLUGIN_DIR / "index.js", "application/javascript")
        elif path == "/api/directives":
            caps_lock_state = load_caps_lock_state()
            
            transcript_path = find_active_transcript()
            user_prompts = []
            if transcript_path:
                user_prompts = extract_user_prompts(transcript_path)
            
            if user_prompts:
                latest_prompt = user_prompts[-1]
                if is_caps_lock_prompt(latest_prompt):
                    if caps_lock_state.get("superprompt") != latest_prompt:
                        caps_lock_state["active"] = True
                        caps_lock_state["superprompt"] = latest_prompt
                        save_caps_lock_state(caps_lock_state)

            custom_path = PLUGIN_DIR / "custom_directives.json"
            custom_directives = []
            if custom_path.exists():
                try:
                    with open(custom_path, "r", encoding="utf-8") as f:
                        custom_directives = json.load(f)
                except Exception:
                    pass
            
            self.send_json(200, {
                "custom_directives": custom_directives,
                "caps_lock": caps_lock_state,
                "question_rule": load_question_rule_state()
            })
        elif path == "/api/logs":
            logs_path = PLUGIN_DIR / "audit_logs.json"
            logs = []
            if logs_path.exists():
                try:
                    with open(logs_path, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                except Exception:
                    pass
            self.send_json(200, logs)
        elif path == "/api/tests":
            results = load_test_results()
            if results:
                self.send_json(200, results)
            else:
                self.send_json(200, {"status": "idle", "tests": []})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == "/api/directives":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                new_directive = data.get("directive", "").strip()
            except Exception:
                self.send_json(400, {"error": "Invalid JSON"})
                return

            if not new_directive:
                self.send_json(400, {"error": "Directive cannot be empty"})
                return

            custom_path = PLUGIN_DIR / "custom_directives.json"
            custom_directives = []
            if custom_path.exists():
                try:
                    with open(custom_path, "r", encoding="utf-8") as f:
                        custom_directives = json.load(f)
                except Exception:
                    pass

            if new_directive not in custom_directives:
                custom_directives.append(new_directive)
                try:
                    with open(custom_path, "w", encoding="utf-8") as f:
                        json.dump(custom_directives, f, indent=2)
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to save directive: {e}"})
                    return

            self.send_json(200, {
                "status": "success", 
                "directives": custom_directives,
                "custom_directives": custom_directives
            })
        elif path == "/api/capslock/cancel":
            state = load_caps_lock_state()
            state["active"] = False
            state["superprompt"] = ""
            save_caps_lock_state(state)
            self.send_json(200, {"status": "success", "caps_lock": state})
        elif path == "/api/question-rule":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                active = bool(data.get("active", False))
            except Exception:
                self.send_json(400, {"error": "Invalid JSON"})
                return
            state = {"active": active}
            save_question_rule_state(state)
            self.send_json(200, {"status": "success", "question_rule": state})
        elif path == "/api/tests":
            results = load_test_results()
            if results:
                self.send_json(200, results)
            else:
                self.send_json(200, {"status": "idle", "tests": []})
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == "/api/directives":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                old_directive = data.get("old_directive", "").strip()
                new_directive = data.get("new_directive", "").strip()
            except Exception:
                self.send_json(400, {"error": "Invalid JSON"})
                return

            if not old_directive or not new_directive:
                self.send_json(400, {"error": "Directives cannot be empty"})
                return

            custom_path = PLUGIN_DIR / "custom_directives.json"
            custom_directives = []
            if custom_path.exists():
                try:
                    with open(custom_path, "r", encoding="utf-8") as f:
                        custom_directives = json.load(f)
                except Exception:
                    pass

            if old_directive in custom_directives:
                idx = custom_directives.index(old_directive)
                if new_directive in custom_directives and new_directive != old_directive:
                    custom_directives.remove(old_directive)
                else:
                    custom_directives[idx] = new_directive
                
                try:
                    with open(custom_path, "w", encoding="utf-8") as f:
                        json.dump(custom_directives, f, indent=2)
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to save directive: {e}"})
                    return

            self.send_json(200, {
                "status": "success",
                "directives": custom_directives,
                "custom_directives": custom_directives
            })
        elif path == "/api/tests":
            results = load_test_results()
            if results:
                self.send_json(200, results)
            else:
                self.send_json(200, {"status": "idle", "tests": []})
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == "/api/directives":
            query_params = parse_qs(parsed_path.query)
            target = query_params.get("directive", [None])[0]

            if not target:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length > 0:
                    body = self.rfile.read(content_length)
                    try:
                        data = json.loads(body)
                        target = data.get("directive")
                    except Exception:
                        pass

            if not target:
                self.send_json(400, {"error": "Directive parameter missing"})
                return

            target = target.strip()
            custom_path = PLUGIN_DIR / "custom_directives.json"
            custom_directives = []
            if custom_path.exists():
                try:
                    with open(custom_path, "r", encoding="utf-8") as f:
                        custom_directives = json.load(f)
                except Exception:
                    pass

            if target in custom_directives:
                custom_directives.remove(target)
                try:
                    with open(custom_path, "w", encoding="utf-8") as f:
                        json.dump(custom_directives, f, indent=2)
                except Exception as e:
                    self.send_json(500, {"error": f"Failed to save directives: {e}"})
                    return

            self.send_json(200, {
                "status": "success",
                "directives": custom_directives,
                "custom_directives": custom_directives
            })
        elif path == "/api/tests":
            results = load_test_results()
            if results:
                self.send_json(200, results)
            else:
                self.send_json(200, {"status": "idle", "tests": []})
        else:
            self.send_response(404)
            self.end_headers()

def main():
    print(f"🚀 Starting Uppercase Enforcer Server on http://localhost:{PORT}...")
    server = ThreadingHTTPServer(('0.0.0.0', PORT), EnforcerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped.")
        sys.exit(0)

if __name__ == "__main__":
    main()
