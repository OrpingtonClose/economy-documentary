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


def _forward_stream(stream, prefix, log_file):
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
        for line in iter(stream.readline, ""):
            if not line:
                break
            log_file.write(line)
            log_file.flush()
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
        self.capabilities = capabilities
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
        import inspect
        self.test_module = ""
        frame = inspect.currentframe()
        while frame:
            glob = frame.f_globals
            module_name = glob.get("__name__", "")
            if "test_" in module_name or module_name.startswith("tests.units"):
                self.test_module = module_name
                break
            frame = frame.f_back

        import json
        config_data = {
            "log_dir": db_dir,
            "max_concurrent_llm": 4,
            "gpu_concurrency": 4,
            "tts_concurrency": 4
        }
        
        # Default to DryRunModel if deepseek key is missing
        deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
        if not os.path.exists(deepseek_key_path):
            config_data["capabilities"] = ["DryRunModel"]
            
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

log_dir = "/tmp/documentary-pipeline"
if os.path.exists("/tmp/active_pipeline_log_dir.txt"):
    try:
        with open("/tmp/active_pipeline_log_dir.txt") as pf:
            val = pf.read().strip()
            if val:
                log_dir = val
    except Exception:
        pass
log_path = os.path.join(log_dir, "vastai_invocations.log")
with open(log_path, "a") as f:
    f.write(cmd_str + "\\n")

# Check if real API key exists to delegate to real CLI
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
    print("ID       Status   IP          Port  GPU       VRAM  Hourly")
    print("1234567  running  127.0.0.1   9001  RTX 4090  24.0  0.85")
elif "copy" in cmd_str:
    print("Copying files from cloud sync connection... 100% complete.")
elif "destroy instance" in cmd_str:
    print("Destroying instance 1234567... Destroyed.")
else:
    print("Mock vastai success")
"""
        with open(vastai_path, "w") as f:
            f.write(mock_vastai_script)
        os.chmod(vastai_path, 0o755)
        print(f"      [Harness] Mock vastai script installed at {vastai_path}")

        # 2. Allocate dynamic ports
        for agent in ["gsa", "scenario", "audio", "video", "provisioner", "assembly"]:
            self.ports[agent] = self._find_free_port()
        print(f"      [Harness] Allocated ports: {self.ports}")

        # 3. Spawn background servers with process group isolation
        import json
        with open("/tmp/active_pipeline_log_dir.txt", "w", encoding="utf-8") as f:
            f.write(db_dir)
        with open("/tmp/active_pipeline_ports.json", "w", encoding="utf-8") as f:
            json.dump(self.ports, f)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        print(f"      [Harness] Spawning background server processes for {self.required_agents}...")
        for agent in self.required_agents:
            port = self.ports[agent]
            
            if agent == "gsa":
                cmd = [sys.executable, "global_state_agent.py", str(port)]
                cwd = str(PROJECT_ROOT / "server")
            else:
                cmd = [sys.executable, f"agents/{agent}/app.py", str(port), self.test_module]
                cwd = str(PROJECT_ROOT / "server")
            
            # Create isolated log files for this agent
            stdout_path = os.path.join(db_dir, f"agent_{agent}_stdout.log")
            stderr_path = os.path.join(db_dir, f"agent_{agent}_stderr.log")
            out_f = open(stdout_path, "w")
            err_f = open(stderr_path, "w")
            self.log_files.extend([out_f, err_f])

            # Start each process in its own group via preexec_fn=os.setsid
            p = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                preexec_fn=os.setsid,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            self.processes.append(p)
            print(f"      [Harness]   - Spawned '{agent}' agent (PID: {p.pid}) on port {port}")

            # Start background forwarding threads to stream stdout and stderr
            t_out = threading.Thread(
                target=_forward_stream,
                args=(p.stdout, f"{agent.upper()}-OUT", out_f),
                daemon=True
            )
            t_err = threading.Thread(
                target=_forward_stream,
                args=(p.stderr, f"{agent.upper()}-ERR", err_f),
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
