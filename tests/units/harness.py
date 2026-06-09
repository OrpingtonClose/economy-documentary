import os
import sys
import time
import socket
import signal
import tempfile
import subprocess
import httpx
import builtins
import threading
from pathlib import Path
from contextlib import ExitStack

# Capability simulator imports removed from harness to keep it generic.
# Simulator classes are statically imported in test scripts.

def print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\n')
    msg = sep.join(str(arg) for arg in args) + end
    if sys.stdout is not None:
        sys.stdout.write(msg)
        sys.stdout.flush()
    else:
        builtins.print(*args, **kwargs)


def _tail_file(file_path, prefix):
    colors = {
        "GSA": "\033[36m",
        "SCENARIO": "\033[34m",
        "AUDIO": "\033[33m",
        "VIDEO": "\033[35m",
        "PROVISIONER": "\033[32m",
        "ASSEMBLY": "\033[31m",
    }
    agent_key = prefix.split("-")[0]
    color = colors.get(agent_key, "\033[0m")
    reset = "\033[0m"
    try:
        import time
        # Wait up to 5 seconds for file creation
        for _ in range(100):
            if os.path.exists(file_path):
                break
            time.sleep(0.05)
        if not os.path.exists(file_path):
            return
            
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.05)
                    continue
                if sys.stdout is not None:
                    sys.stdout.write(f"{color}[{prefix}] {line}{reset}")
                    sys.stdout.flush()
                else:
                    builtins.print(f"{color}[{prefix}] {line}{reset}", end="")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class IntegrationHarness:
    def __init__(self, required_agents: list[str] = None, capabilities: list[str] = None):
        """
        Setup process-isolated testing harness.
        
        Args:
            required_agents: List of agent names to spawn. Defaults to all 6 if None.
            capabilities: Custom list of capability strings.
        """
        self.required_agents = required_agents or ["gsa", "scenario", "audio", "video", "provisioner", "assembly"]
        self.capabilities = capabilities if capabilities is not None else ["DryRunModel"]
        self.temp_dir = None
        self.ports = {}
        self.processes = []
        self.log_files = []
        self.exit_stack = None

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def __enter__(self):
        self.exit_stack = ExitStack()
        
        # 1. Create unique isolated directory for this test run
        self.temp_dir = tempfile.TemporaryDirectory()
        self.exit_stack.enter_context(self.temp_dir)
        db_dir = self.temp_dir.name
        print(f"      [Harness] Setup temporary isolated directory at: {db_dir}")
        
        # Get caller module name to pass to agent scripts
        self.test_module = ""
        env_test = os.environ.get("PYTEST_CURRENT_TEST", "")
        if env_test:
            file_part = env_test.split("::")[0]
            path_str = file_part.replace(".py", "")
            parts = path_str.replace("\\", "/").split("/")
            if "tests" in parts:
                idx = parts.index("tests")
                self.test_module = ".".join(parts[idx:])
            else:
                self.test_module = parts[-1]
        
        if not self.test_module:
            import inspect
            for frame_info in inspect.stack():
                filename = frame_info.filename
                if filename:
                    basename = os.path.basename(filename)
                    if basename.startswith("test_") and basename.endswith(".py"):
                        try:
                            rel_path = Path(filename).resolve().relative_to(PROJECT_ROOT)
                            self.test_module = rel_path.with_suffix("").as_posix().replace("/", ".")
                        except ValueError:
                            self.test_module = Path(filename).stem
                        break
        
        if not self.test_module:
            for arg in sys.argv:
                if "test_" in arg and arg.endswith(".py"):
                    path_str = arg.replace(".py", "")
                    parts = path_str.replace("\\", "/").split("/")
                    if "tests" in parts:
                        idx = parts.index("tests")
                        self.test_module = ".".join(parts[idx:])
                    else:
                        self.test_module = parts[-1]
                    break

        # 2. Allocate dynamic ports
        for agent in ["gsa", "scenario", "audio", "video", "provisioner", "assembly"]:
            self.ports[agent] = self._find_free_port()
        print(f"      [Harness] Allocated ports: {self.ports}")

        import json
        config_data = {
            "log_dir": db_dir,
            "max_concurrent_llm": 4,
            "gpu_concurrency": 4,
            "tts_concurrency": 4,
            "ports": self.ports
        }
        
        # Seed configured capabilities (defaults to ['DryRunModel'])
        config_data["capabilities"] = self.capabilities
            
        config_path = os.path.join(db_dir, "run_config.json")
        with open(config_path, "w") as f:
            json.dump(config_data, f)
        print(f"      [Harness] Seeded run_config.json")

        # Setup mock bin path for vastai CLI testing
        bin_dir = os.path.join(db_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        vastai_path = os.path.join(bin_dir, "vastai")
        
        mock_vastai_script = """#!/usr/bin/env python3
import sys
import json
import os
import subprocess

args = sys.argv[1:]
cmd_str = " ".join(args)

log_dir = "{db_dir}"
log_path = os.path.join(log_dir, "vastai_invocations.log")
with open(log_path, "a") as f:
    f.write(cmd_str + "\\n")

# Check if we should delegate to real CLI
use_real = False
run_config_path = os.path.join(log_dir, "run_config.json")
if os.path.exists(run_config_path):
    try:
        with open(run_config_path) as rf:
            cfg = json.load(rf)
            if "capabilities" in cfg and "VastRealCapability" in cfg["capabilities"]:
                use_real = True
    except Exception:
        pass

if use_real:
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if os.path.exists(vast_key_path):
        with open(vast_key_path) as kf:
            api_key = kf.read().strip()
        real_vastai = "/Users/orpington/.letta-cli-venv/bin/vastai"
        exec_args = [real_vastai]
        if "--api-key" not in args:
            exec_args += ["--api-key", api_key]
        exec_args += args
        res = subprocess.run(exec_args, capture_output=True, text=True)
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        sys.exit(res.returncode)

if "search offers" in cmd_str:
    print("ID      CUDA   GPU_name       Num_GPUs  VRAM   Inet_up  Inet_down  Reliability  Price")
    print("1001    12.0   RTX 3090       1         24.0   100.0    100.0      0.99         0.45")
    print("1002    12.0   RTX 4090       1         24.0   150.0    150.0      0.99         0.85")
elif "create instance" in cmd_str:
    print("Started. Instance ID: 1234567")
elif "show instances" in cmd_str or "show instance" in cmd_str:
    import sqlite3
    db_path = os.path.join(log_dir, "events.db")
    allocated = set()
    deallocated = set()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT effect_data FROM events")
            for row in c.fetchall():
                try:
                    evt = json.loads(row[0])
                    inst_id = evt.get("instance_id")
                    if inst_id:
                        if evt.get("kind") == "vm_allocated":
                            allocated.add(inst_id)
                        elif evt.get("kind") == "vm_deallocated":
                            deallocated.add(inst_id)
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass
    active_instances = allocated - deallocated
    if not active_instances:
        active_instances = {"1234567"}
    
    print("ID       Status   IP          Port  GPU       VRAM  Hourly")
    for inst in sorted(active_instances):
        gpu = "RTX A6000" if inst == "7654321" else "RTX 4090"
        rate = "0.40" if inst == "7654321" else "0.85"
        print(f"{inst}  running  127.0.0.1   9001  {gpu}  24.0  {rate}")
elif "copy" in cmd_str:
    print("Copying files from cloud sync connection... 100% complete.")
elif "destroy instance" in cmd_str:
    inst_to_destroy = "1234567"
    for word in args:
        if word.isdigit():
            inst_to_destroy = word
    print(f"Destroying instance {inst_to_destroy}... Destroyed.")
else:
    print("Mock vastai success")
""".replace("{db_dir}", db_dir)
        with open(vastai_path, "w") as f:
            f.write(mock_vastai_script)
        os.chmod(vastai_path, 0o755)
        print(f"      [Harness] Mock vastai script installed at {vastai_path}")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        print(f"      [Harness] Spawning background server processes for {self.required_agents}...")
        for agent in self.required_agents:
            port = self.ports[agent]
            
            stdout_path = os.path.join(db_dir, f"agent_{agent}_stdout.log")
            stderr_path = os.path.join(db_dir, f"agent_{agent}_stderr.log")
            
            cmd = ["bash", "launch_agent.sh", sys.executable, agent, str(port), self.test_module or "", stdout_path, stderr_path, db_dir]
            cwd = str(PROJECT_ROOT / "server")
            
            res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True)
            pid = int(res.stdout.strip())
            
            class SpawnedProcess:
                def __init__(self, pid):
                    self.pid = pid
                def wait(self):
                    import time
                    for _ in range(50):
                        try:
                            os.kill(self.pid, 0)
                        except OSError:
                            break
                        time.sleep(0.05)
                def kill(self):
                    import signal
                    try:
                        os.kill(self.pid, signal.SIGKILL)
                    except Exception:
                        pass

            self.processes.append(SpawnedProcess(pid))
            print(f"      [Harness]   - Spawned '{agent}' agent (PID: {pid}) on port {port} via launch_agent.sh")

            # Start background forwarding threads to stream log files in real-time
            t_out = threading.Thread(
                target=_tail_file,
                args=(stdout_path, f"{agent.upper()}-OUT"),
                daemon=True
            )
            t_err = threading.Thread(
                target=_tail_file,
                args=(stderr_path, f"{agent.upper()}-ERR"),
                daemon=True
            )
            t_out.start()
            t_err.start()

        # 4. Wait for all servers to become healthy
        print(f"      [Harness] Probing agent health endpoints...")
        for agent in self.required_agents:
            port = self.ports[agent]
            healthy = False
            delay = 0.1
            for attempt in range(30):
                try:
                    resp = httpx.get(f"http://127.0.0.1:{port}/")
                    if resp.status_code in (200, 400, 404, 500):
                        healthy = True
                        break
                except Exception:
                    pass
                time.sleep(delay)
                delay = min(delay * 1.5, 1.5)
            
            if not healthy:
                print(f"      [Harness]   ❌ Agent '{agent}' failed to become healthy on port {port}!")
                stderr_path = os.path.join(db_dir, f"agent_{agent}_stderr.log")
                stdout_path = os.path.join(db_dir, f"agent_{agent}_stdout.log")
                if os.path.exists(stderr_path):
                    print(f"\n--- FAILED AGENT '{agent.upper()}' STDERR LOG ---")
                    try:
                        with open(stderr_path) as lf:
                            print(lf.read())
                    except Exception as exc:
                        print(f"Failed to read stderr log: {exc}")
                if os.path.exists(stdout_path):
                    print(f"\n--- FAILED AGENT '{agent.upper()}' STDOUT LOG ---")
                    try:
                        with open(stdout_path) as lf:
                            print(lf.read())
                    except Exception as exc:
                        print(f"Failed to read stdout log: {exc}")
                self.__exit__(None, None, None)
                raise RuntimeError(f"Server '{agent}' on port {port} failed to start healthily.")
            else:
                print(f"      [Harness]   ✓ Agent '{agent}' is healthy")

        print("      [Harness] All required background servers are running and healthy")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # An error occurred! Dump the agent stderr logs for troubleshooting
            print("\n" + "!" * 80)
            print("                 INTEGRATION HARNESS DIAGNOSTIC DUMP")
            print("!" * 80)
            if self.temp_dir:
                db_dir = self.temp_dir.name
                for agent in ["gsa", "scenario", "audio", "video", "provisioner", "assembly"]:
                    stderr_path = os.path.join(db_dir, f"agent_{agent}_stderr.log")
                    stdout_path = os.path.join(db_dir, f"agent_{agent}_stdout.log")
                    if os.path.exists(stderr_path):
                        print(f"\n--- AGENT '{agent.upper()}' STDERR LOG (LAST 40 LINES) ---")
                        try:
                            with open(stderr_path) as f:
                                lines = f.readlines()
                                for line in lines[-40:]:
                                    print("  " + line.strip())
                        except Exception as exc:
                            print(f"Failed to read agent log: {exc}")
                    if os.path.exists(stdout_path):
                        print(f"\n--- AGENT '{agent.upper()}' STDOUT LOG (LAST 40 LINES) ---")
                        try:
                            with open(stdout_path) as f:
                                lines = f.readlines()
                                for line in lines[-40:]:
                                    print("  " + line.strip())
                        except Exception as exc:
                            print(f"Failed to read agent log: {exc}")
            print("!" * 80 + "\n")

        print(f"      [Harness] Tearing down harness: killing {len(self.processes)} spawned background processes...")
        # 1. Kill spawned process groups cleanly and forcefully
        for p in self.processes:
            try:
                pgid = os.getpgid(p.pid)
                print(f"      [Harness]   - Sending SIGKILL to process group {pgid} (parent PID {p.pid})...")
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            p.wait()

        self.processes.clear()

        # Close all open log file descriptors
        for f in self.log_files:
            try:
                f.close()
            except Exception:
                pass
        self.log_files.clear()

        # Cleanup active config files
        for path in ["/tmp/active_pipeline_log_dir.txt", "/tmp/active_pipeline_ports.json"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        # Cleanup test_capabilities.py bridge file
        bridge_path = PROJECT_ROOT / "server" / "test_capabilities.py"
        if bridge_path.exists():
            try:
                bridge_path.unlink()
                print(f"      [Harness] Deleted bridge capabilities file: {bridge_path}")
            except Exception:
                pass

        # 2. Cleanup TemporaryDirectory context
        if self.exit_stack:
            self.exit_stack.close()
            self.exit_stack = None
        print("      [Harness] Teardown complete. Temporary files cleaned up.")
