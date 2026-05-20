#!/usr/bin/env python3
"""
Debug-Gym Agent — LLM overseer that monitors pipeline execution in real-time.

Consumes log events, explores code, checks VM state, and AUTO-INTERVENES when
contract drift is detected. Inspired by Microsoft Research debug-gym FreeEnv.

OPERATING PRINCIPLES (DO NOT VIOLATE):
1. OBSERVE AND REPORT — never kill a healthy pipeline. VM boot time (2-4 min)
   is NORMAL. "Offline" status during first 180s is NOT a failure.
2. ONLY FIX REAL BUGS — when the pipeline crashes, use tools to explore code,
   understand the root cause, and report findings. Do NOT add complexity.
3. NO FALSE POSITIVES — "production stage active but zero clips" during VM
   provisioning is NOT a bug. Wait for workers to bootstrap.
4. MINIMAL INTERVENTION — if budget >95% or VM truly dead (>300s), THEN act.
   Otherwise: watch, log, and let the human decide.
5. NEVER ADD FEATURES — this is a debugger, not a product. No new directives,
   no new tools, no new abstractions. Fix what's broken. Nothing more.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


logger = logging.getLogger("debug_gym")


# ── Architecture Contract (distilled from docs/ARCHITECTURE.md) ──
ARCHITECTURE_CONTRACT = {
    "stages": ["scenario", "audio", "visual", "production", "assembly"],
    "stage_order": "strictly_sequential",
    "max_vms": 3,
    "budget_soft_limit_pct": 80,
    "budget_hard_limit_pct": 95,
    "dependencies": {
        "whisperx": {"required": True, "install": "pip install whisperx"},
        "opentimelineio": {"required": True},
        "torch": {"required": True},
    },
    "artifacts": {
        "audio": {"min_clips": 1, "format": "wav"},
        "video": {"min_clips": 1, "format": "mp4"},
        "master": {"required": True, "format": "mp4", "name": "master.mp4"},
    },
    "invariants": [
        "no_time_stretching",
        "no_frozen_frames",
        "no_looping_media",
        "media_immutable_once_created",
    ],
}


@dataclass
class Observation:
    """A snapshot of pipeline state for the agent."""
    timestamp: float
    log_lines: list[str]
    vm_count: int = 0
    vm_cost_hr: float = 0.0
    audio_clips: int = 0
    video_clips: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    current_stage: str = ""
    budget_spent_pct: float = 0.0


class ToolExecutor:
    """Executes tools the LLM agent can call."""

    def __init__(self, repo_root: str, output_dir: str) -> None:
        self.repo_root = repo_root
        self.output_dir = output_dir

    def view(self, path: str, offset: int = 1, n_lines: int = 50) -> str:
        """Read a source file."""
        full = os.path.join(self.repo_root, path)
        try:
            with open(full) as f:
                lines = f.readlines()
            start = max(0, offset - 1)
            end = min(len(lines), start + n_lines)
            return "".join(lines[start:end])
        except Exception as e:
            return f"Error reading {path}: {e}"

    def grep(self, pattern: str, path: str = "") -> str:
        """Search code."""
        target = os.path.join(self.repo_root, path) if path else self.repo_root
        try:
            result = subprocess.run(
                ["grep", "-rn", pattern, target],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.strip().split("\n")[:20]
            return "\n".join(lines) if lines else "No matches"
        except Exception as e:
            return f"grep error: {e}"

    def bash(self, cmd: str) -> str:
        """Run a shell command."""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30,
            )
            out = result.stdout[-2000:] if result.stdout else ""
            err = result.stderr[-500:] if result.stderr else ""
            return f"stdout:\n{out}\nstderr:\n{err}"[:2500]
        except Exception as e:
            return f"bash error: {e}"

    def _vast_key(self) -> str:
        return os.environ.get("VAST_API_KEY", "") or os.environ.get("VAST_AI_KEY", "")

    def vm_status(self) -> str:
        """Check Vast.ai VMs."""
        try:
            key = self._vast_key()
            if not key:
                return "VAST_API_KEY/VAST_AI_KEY not set"
            result = subprocess.run(
                ["curl", "-sL", "-H", f"Authorization: Bearer {key}",
                 "https://console.vast.ai/api/v0/instances/"],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(result.stdout)
            instances = data.get("instances", [])
            active = [i for i in instances
                      if i.get("actual_status") not in ("exited", "offline")]
            total_cost = sum(i.get("dph_total", 0) for i in active)
            lines = [f"Active VMs: {len(active)}, total cost: ${total_cost:.2f}/hr"]
            for i in active:
                lines.append(
                    f"  {i.get('id')} {i.get('actual_status')} "
                    f"{i.get('gpu_name')} ${i.get('dph_total',0):.2f}/hr"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"vm_status error: {e}"

    def tail_log(self, n_lines: int = 50) -> str:
        """Tail pipeline log."""
        log_path = os.path.join(self.output_dir, "pipeline_live.log")
        try:
            result = subprocess.run(
                ["tail", "-n", str(n_lines), log_path],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout
        except Exception as e:
            return f"tail error: {e}"

    # ── Auto-Intervention Tools ──

    def install_dependency(self, package: str) -> str:
        """Install a Python package via pip."""
        logger.info("[AUTO] Installing dependency: %s", package)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", package],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                return json.dumps({"status": "installed", "package": package})
            return json.dumps({"status": "failed", "package": package, "error": result.stderr[-500:]})
        except Exception as exc:
            return json.dumps({"status": "failed", "package": package, "error": str(exc)})

    def patch_code(self, path: str, old: str, new: str) -> str:
        """Patch a source file by replacing old with new."""
        full = os.path.join(self.repo_root, path)
        logger.info("[AUTO] Patching %s", path)
        try:
            with open(full) as f:
                content = f.read()
            if old not in content:
                return json.dumps({"status": "failed", "error": "old text not found in file"})
            content = content.replace(old, new, 1)
            with open(full, "w") as f:
                f.write(content)
            return json.dumps({"status": "patched", "path": path})
        except Exception as exc:
            return json.dumps({"status": "failed", "error": str(exc)})

    def adjust_config(self, path: str, key: str, value: Any) -> str:
        """Adjust a config value in a JSON or Python file."""
        full = os.path.join(self.repo_root, path)
        logger.info("[AUTO] Adjusting config %s: %s = %s", path, key, value)
        try:
            if path.endswith(".json"):
                with open(full) as f:
                    cfg = json.load(f)
                cfg[key] = value
                with open(full, "w") as f:
                    json.dump(cfg, f, indent=2)
                return json.dumps({"status": "adjusted", "path": path, "key": key})
            # Simple regex replace for Python files
            with open(full) as f:
                content = f.read()
            # Match KEY = value (various forms)
            pattern = rf'({re.escape(key)}\s*=\s*)([^\n#]+)'
            repl = rf'\g<1>{repr(value)}'
            new_content, count = re.subn(pattern, repl, content)
            if count == 0:
                return json.dumps({"status": "failed", "error": f"key '{key}' not found"})
            with open(full, "w") as f:
                f.write(new_content)
            return json.dumps({"status": "adjusted", "path": path, "key": key})
        except Exception as exc:
            return json.dumps({"status": "failed", "error": str(exc)})

    def kill_pipeline(self) -> str:
        """DISABLED — never kill a running pipeline. Use patch_code + reload_module instead."""
        return json.dumps({"status": "disabled", "message": "kill_pipeline is disabled. Hot-swap fixes with patch_code + reload_module."})

    def restart_pipeline(self, brief: str, api_key: str, output_dir: str) -> str:
        """DISABLED — never restart pipelines. Hot-swap fixes instead."""
        return json.dumps({"status": "disabled", "message": "restart_pipeline is disabled. Hot-swap fixes with patch_code + reload_module."})

    def constrain_scope(self, max_scenes: int, max_duration_seconds: int) -> str:
        """Write a scope constraint directive for the scenario agent."""
        directive = {
            "from": "debug_gym",
            "target_agent": "scenario_agent",
            "action": "constrain_scope",
            "max_scenes": int(max_scenes),
            "max_duration_seconds": int(max_duration_seconds),
        }
        path = os.path.join(self.output_dir, ".directives.json")
        try:
            with open(path, "w") as f:
                json.dump(directive, f, indent=2)
            logger.info("[DIRECTIVE] Wrote constrain_scope: %s", directive)
            return json.dumps({"status": "ok", "directive": directive})
        except Exception as exc:
            logger.error("Failed to write directive: %s", exc)
            return json.dumps({"status": "failed", "error": str(exc)})

    def reload_module(self, module_path: str) -> str:
        """Reload a Python module so runtime code patches take effect.

        module_path: dot-separated import path, e.g. 'strands_agents.stages.audio_stage'
        """
        logger.info("[AUTO] Reloading module: %s", module_path)
        try:
            import importlib
            import sys
            # Ensure repo is on path
            server_dir = os.path.join(self.repo_root, "server")
            if server_dir not in sys.path:
                sys.path.insert(0, server_dir)
            mod = importlib.import_module(module_path)
            importlib.reload(mod)
            return json.dumps({"status": "reloaded", "module": module_path})
        except Exception as exc:
            return json.dumps({"status": "failed", "module": module_path, "error": str(exc)})


class ContractDriftDetector:
    """Detects drift between observed pipeline state and architecture contract."""

    def __init__(self, contract: dict) -> None:
        self.contract = contract
        self._stage_history: list[str] = []

    def detect(self, obs: Observation) -> list[dict]:
        """Return list of drift findings."""
        drifts = []

        # 1. VM limit drift
        if obs.vm_count > self.contract["max_vms"]:
            drifts.append({
                "severity": "CRITICAL",
                "clause": "VM Provisioning: max 3 VMs",
                "finding": f"Detected {obs.vm_count} active VMs (max={self.contract['max_vms']})",
                "fix": "patch provisioner timeout + reload_module('worker_provisioner')",
                "auto_action": "report",
            })

        # 2. Budget drift
        if obs.budget_spent_pct > self.contract["budget_hard_limit_pct"]:
            drifts.append({
                "severity": "CRITICAL",
                "clause": "Budget: hard limit at 95%",
                "finding": f"Budget at {obs.budget_spent_pct:.0f}% (hard limit={self.contract['budget_hard_limit_pct']}%)",
                "fix": "report_drift + patch_code to limit scope, do NOT kill",
                "auto_action": "kill",
            })
        elif obs.budget_spent_pct > self.contract["budget_soft_limit_pct"]:
            drifts.append({
                "severity": "HIGH",
                "clause": "Budget: soft warning at 80%",
                "finding": f"Budget at {obs.budget_spent_pct:.0f}%",
                "fix": "warn and monitor closely",
                "auto_action": "report",
            })

        # 3. Dependency drift — WhisperX
        whisperx_missing = any(
            "WhisperX is not installed" in e or "No module named 'whisperx'" in e
            for e in obs.errors
        )
        if whisperx_missing:
            drifts.append({
                "severity": "HIGH",
                "clause": "Dependencies: whisperx required",
                "finding": "WhisperX not installed — audio alignment blocked",
                "fix": "install_dependency('whisperx') or use synthetic timing",
                "auto_action": "install_dep",
                "package": "whisperx",
            })

        # 4. Stage loop drift — only flag if stage CHANGES and comes back
        if obs.current_stage:
            # Only append on actual stage transitions, not repeated observations
            if not self._stage_history or self._stage_history[-1] != obs.current_stage:
                self._stage_history.append(obs.current_stage)
            from collections import Counter
            counts = Counter(self._stage_history)
            for stage, count in counts.items():
                if count > 2:
                    drifts.append({
                        "severity": "CRITICAL",
                        "clause": "Stage Flow: each stage runs once",
                        "finding": f"Stage '{stage}' cycled {count} times (detected via transitions)",
                        "fix": "investigate gate validation or backward edges",
                        "auto_action": "report",
                    })

        # 5. Error storm drift
        error_count = len(obs.errors)
        if error_count > 5:
            drifts.append({
                "severity": "HIGH",
                "clause": "Error Rate: sustained errors",
                "finding": f"{error_count} errors in last observation window",
                "fix": "grep for error pattern, patch source, or escalate",
                "auto_action": "report",
            })

        # 6. No output drift — only flag if we're PAST provisioning
        # (VM bootstrapping can take 2-3 minutes; "offline" is normal during boot)
        provisioning_lines = [l for l in obs.log_lines if "Provisioning" in l or "Waiting for" in l or "bootstrap" in l.lower()]
        if (obs.audio_clips == 0 and obs.video_clips == 0
                and obs.current_stage == "production"
                and not provisioning_lines):
            drifts.append({
                "severity": "MEDIUM",
                "clause": "Output Verification: clips must be generated",
                "finding": "Production stage active but zero clips produced (provisioning complete)",
                "fix": "check VM health and worker connectivity",
                "auto_action": "report",
            })

        return drifts


class DebugGymAgent:
    """LLM agent that monitors pipeline execution and auto-intervenes."""

    SYSTEM_PROMPT = """\
You are a debug-gym architecture overseer. You monitor a documentary pipeline
running on GPU VMs and AUTO-INTERVENE when contract drift is detected.

CONTRACT RULES:
1. Stage order: scenario → audio → video → assembly (each once)
2. VM limit: max 3 active VMs at any time
3. Budget: soft warn at 80%, hard stop at 95%
4. Dependencies: whisperx, opentimelineio, torch must be available
5. Cleanup: VMs destroyed in finally + SIGINT
6. No silent errors — every failure must be reported or fixed
7. Output: master.mp4 must exist on success
8. No time-stretching, frozen frames, or looping media

VM BOOT POLICY (CRITICAL — DO NOT VIOLATE):
- VMs can take 2-4 minutes to boot. "offline" status during first 180s is NORMAL.
- The pipeline has a 300s timeout for VM startup. Do NOT kill before 240s.
- A VM showing "offline" for < 180s is NOT a failure. Wait.
- Only consider a VM failed if offline > 240s OR if bootstrap errors appear in log.

AUTO-INTERVENTION POLICY:
When drift is detected, you MUST act immediately without waiting for human
prompt. Use these tools:

- install_dependency(package) — pip install missing packages
- patch_code(path, old, new) — fix bugs in source files. Use RELATIVE paths from repo root, e.g. 'server/tools/video_tools.py' or 'scripts/run_pipeline_with_audit.py'
- adjust_config(path, key, value) — change config values
- report_drift(clause, finding, fix) — log the violation
- constrain_scope(max_scenes, max_duration_seconds) — limit pipeline scope
- reload_module(module_path) — reload a Python module after patching code so changes take effect WITHOUT restarting the pipeline. Use dot-paths like 'strands_agents.stages.audio_stage' or 'tools.video_tools'

DECISION RULES:
- CRITICAL drift → act immediately (kill, patch, or install)
- HIGH drift → act if safe (install dep, patch code)
- MEDIUM drift → investigate first, then act
- NEVER kill or restart the pipeline. Always use patch_code + reload_module for hot-swapping fixes
- Always report drift after acting

RESPONSE FORMAT:
Thought: <what drift you detected and why>
Action: <tool_name>(arg1=val1, arg2=val2)
"""

    def __init__(
        self,
        repo_root: str,
        output_dir: str,
        api_key: str,
        model: str = "deepseek-chat",
    ) -> None:
        self.tools = ToolExecutor(repo_root, output_dir)
        self.detector = ContractDriftDetector(ARCHITECTURE_CONTRACT)
        self.api_key = api_key
        self.model = model
        self.output_dir = output_dir
        self._stop = threading.Event()
        self._drift_count = 0
        self._intervention_count = 0
        self._budget_start = None

    def _call_llm(self, messages: list[dict]) -> str:
        """Call the LLM."""
        import httpx
        resp = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 600,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _parse_action(self, text: str) -> tuple[str, dict]:
        """Parse Action: tool_name(args) from LLM response.
        Handles quoted strings containing commas.
        """
        match = re.search(r"Action:\s*(\w+)\s*\((.*)\)", text, re.DOTALL)
        if not match:
            return "", {}
        tool_name = match.group(1)
        args_str = match.group(2).strip()
        kwargs = {}
        # Parse key=value pairs, respecting double-quoted values with commas
        i = 0
        while i < len(args_str):
            eq = args_str.find("=", i)
            if eq == -1:
                break
            k = args_str[i:eq].strip()
            v_start = eq + 1
            # Skip whitespace
            while v_start < len(args_str) and args_str[v_start] == " ":
                v_start += 1
            if v_start < len(args_str) and args_str[v_start] == '"':
                # Find closing quote
                close = args_str.find('"', v_start + 1)
                if close == -1:
                    close = len(args_str)
                v = args_str[v_start + 1:close]
                i = close + 1
            else:
                # Unquoted value — find next comma or end
                comma = args_str.find(",", v_start)
                if comma == -1:
                    comma = len(args_str)
                v = args_str[v_start:comma].strip()
                i = comma + 1
            kwargs[k] = v.strip('"\'')
            # Skip delimiter commas and whitespace before next key
            while i < len(args_str) and args_str[i] in ", \n\r\t":
                i += 1
        return tool_name, kwargs

    def _execute_tool(self, name: str, kwargs: dict) -> str:
        """Execute a tool by name."""
        fn_map: dict[str, Callable[..., str]] = {
            "view": self.tools.view,
            "grep": self.tools.grep,
            "bash": self.tools.bash,
            "vm_status": self.tools.vm_status,
            "tail_log": self.tools.tail_log,
            "install_dependency": self.tools.install_dependency,
            "patch_code": self.tools.patch_code,
            "adjust_config": self.tools.adjust_config,
            "constrain_scope": self.tools.constrain_scope,
            "reload_module": self.tools.reload_module,
            "report_drift": self._report_drift,
        }
        fn = fn_map.get(name)
        if fn is None:
            return f"Unknown tool: {name}"
        try:
            return fn(**kwargs)
        except Exception as e:
            return f"Tool error: {e}"

    def _build_observation(self) -> Observation:
        """Build current pipeline observation."""
        log_text = self.tools.tail_log(50)
        log_lines = log_text.split("\n") if log_text else []

        audio_dir = os.path.join(self.output_dir, "audio")
        video_dir = os.path.join(self.output_dir, "video")
        audio_clips = 0
        video_clips = 0
        try:
            if os.path.isdir(audio_dir):
                audio_clips = len([f for f in os.listdir(audio_dir) if f.endswith(".wav")])
        except Exception:
            pass
        try:
            if os.path.isdir(video_dir):
                video_clips = len([f for f in os.listdir(video_dir) if f.endswith(".mp4")])
        except Exception:
            pass

        errors = [l for l in log_lines if "ERROR" in l][-10:]
        warnings = [l for l in log_lines if "WARN" in l][-10:]

        vm_text = self.tools.vm_status()
        vm_count = vm_text.count("status=running") + vm_text.count("status=loading")

        # Detect current stage from log (be precise — avoid false positives)
        stage = ""
        for line in reversed(log_lines):
            if "scenario" in line.lower() and "complete" in line.lower():
                stage = "scenario"
                break
            if "audio" in line.lower() and ("generated" in line.lower() or "alignment" in line.lower()):
                stage = "audio"
                break
            # Only call it "production" when actually generating clips,
            # not during VM provisioning or bootstrap
            if any(k in line.lower() for k in ("generating clip", "submit_gpu_production_job", "clip generated", "rendering frame")):
                stage = "production"
                break
            if "assembly" in line.lower() or "master.mp4" in line.lower():
                stage = "assembly"
                break

        # Budget tracking
        budget_pct = 0.0
        if self._budget_start is None:
            self._budget_start = self._get_budget()
        current_budget = self._get_budget()
        if self._budget_start and self._budget_start > 0:
            spent = self._budget_start - current_budget
            budget_pct = (spent / self._budget_start) * 100

        return Observation(
            timestamp=time.time(),
            log_lines=log_lines[-10:],
            vm_count=vm_count,
            audio_clips=audio_clips,
            video_clips=video_clips,
            errors=errors,
            warnings=warnings,
            current_stage=stage,
            budget_spent_pct=budget_pct,
        )

    def _get_budget(self) -> float:
        """Query current Vast.ai balance."""
        try:
            key = self._vast_key()
            if not key:
                return 0.0
            result = subprocess.run(
                ["curl", "-sL", "-H", f"Authorization: Bearer {key}",
                 "https://console.vast.ai/api/v0/user/"],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            credit = float(data.get("credit", 0))
            balance = float(data.get("balance", 0))
            return credit + max(0, balance)
        except Exception:
            return 0.0

    def _vast_key(self) -> str:
        return os.environ.get("VAST_API_KEY", "") or os.environ.get("VAST_AI_KEY", "")

    def _write_directive(self, directive: dict) -> str:
        """Write a directive for pipeline agents to read."""
        path = os.path.join(self.output_dir, ".directives.json")
        try:
            with open(path, "w") as f:
                json.dump(directive, f, indent=2)
            logger.info("[DIRECTIVE] Wrote directive: %s", directive)
            return f"Directive written: {directive.get('action', 'unknown')}"
        except Exception as exc:
            logger.error("Failed to write directive: %s", exc)
            return f"Directive write failed: {exc}"

    def _auto_intervene(self, drift: dict) -> str:
        """Instruct pipeline agents via directive instead of direct action."""
        action = drift.get("auto_action", "report")
        severity = drift.get("severity", "LOW")

        logger.info("[DIRECTIVE] %s: %s — action=%s",
                    severity, drift["finding"], action)

        # Map drift to directive for the relevant agent
        if action == "kill":
            # PASSIVE MODE: never kill, only report
            return self._write_directive({
                "from": "debug_gym",
                "severity": severity,
                "target_agent": "human",
                "action": "report_only",
                "reason": drift["finding"],
                "fix": drift["fix"],
            })

        if action == "install_dep":
            pkg = drift.get("package", "")
            if pkg == "whisperx":
                return self._write_directive({
                    "from": "debug_gym",
                    "severity": severity,
                    "target_agent": "audio_agent",
                    "action": "install_whisperx",
                    "reason": drift["finding"],
                    "fix": drift["fix"],
                })
            return self._write_directive({
                "from": "debug_gym",
                "severity": severity,
                "target_agent": "provisioning_agent",
                "action": f"install_{pkg}",
                "reason": drift["finding"],
                "fix": drift["fix"],
            })

        if "whisperx" in drift.get("finding", "").lower():
            return self._write_directive({
                "from": "debug_gym",
                "severity": severity,
                "target_agent": "audio_agent",
                "action": "use_synthetic_timing",
                "reason": drift["finding"],
                "fix": drift["fix"],
            })

        if "vm" in drift.get("clause", "").lower():
            return self._write_directive({
                "from": "debug_gym",
                "severity": severity,
                "target_agent": "provisioning_agent",
                "action": "destroy_excess_vms",
                "reason": drift["finding"],
                "fix": drift["fix"],
            })

        if "budget" in drift.get("clause", "").lower():
            return self._write_directive({
                "from": "debug_gym",
                "severity": severity,
                "target_agent": "pipeline_runner",
                "action": "abort",
                "reason": drift["finding"],
                "fix": drift["fix"],
            })

        # Default: write investigate directive
        return self._write_directive({
            "from": "debug_gym",
            "severity": severity,
            "target_agent": "all",
            "action": "investigate",
            "reason": drift["finding"],
            "fix": drift["fix"],
        })

    def _report_drift(self, clause: str, finding: str, fix: str) -> str:
        """Report drift to log file."""
        report_path = os.path.join(self.output_dir, "debug_gym_drift.jsonl")
        entry = {
            "timestamp": time.time(),
            "severity": "AUTO",
            "clause": clause,
            "finding": finding,
            "fix": fix,
        }
        with open(report_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return f"Drift reported: {clause}"

    def run(self, interval_sec: float = 30.0) -> None:
        """Main agent loop with auto-intervention."""
        print("=" * 60)
        print("DEBUG-GYM AGENT STARTED (auto-intervention enabled)")
        print("=" * 60)

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
        ]

        while not self._stop.is_set():
            obs = self._build_observation()

            # ── AUTO-INTERVENTION: detect drift without LLM ──
            drifts = self.detector.detect(obs)
            if drifts:
                for drift in drifts:
                    print(f"\n[DRIFT DETECTED] {drift['severity']}: {drift['finding']}")
                    result = self._auto_intervene(drift)
                    print(f"[AUTO-ACTION] {result}")
                    self._intervention_count += 1
                    self._drift_count += 1

            # ── LLM oversight ──
            obs_text = self._observation_to_text(obs)
            messages.append({"role": "user", "content": obs_text})

            try:
                response = self._call_llm(messages)
            except Exception as e:
                logger.error("LLM call failed: %s", e)
                time.sleep(interval_sec)
                continue

            print(f"\n--- Agent Observation ---\n{obs_text[:500]}")
            print(f"\n--- Agent Response ---\n{response[:500]}")

            # Parse and execute action
            tool_name, kwargs = self._parse_action(response)
            if tool_name and tool_name.lower() not in ("none", "", "null"):
                result = self._execute_tool(tool_name, kwargs)
                print(f"\n--- Tool Result ({tool_name}) ---\n{result[:500]}")
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"Tool result:\n{result}"})

                if tool_name in ("patch_code", "reload_module", "install_dependency"):
                    self._intervention_count += 1

            # Trim context
            if len(messages) > 20:
                messages = [messages[0]] + messages[-18:]

            time.sleep(interval_sec)

        print(f"\nAgent stopped. Drifts: {self._drift_count}, Interventions: {self._intervention_count}")

    def _observation_to_text(self, obs: Observation) -> str:
        lines = [
            f"=== Pipeline Observation ({time.strftime('%H:%M:%S', time.localtime(obs.timestamp))}) ===",
            f"Stage: {obs.current_stage} | VMs: {obs.vm_count} | Budget: {obs.budget_spent_pct:.0f}%",
            f"Audio: {obs.audio_clips} | Video: {obs.video_clips}",
            "",
            "Recent log:",
        ]
        for l in obs.log_lines[-5:]:
            lines.append(f"  {l}")
        if obs.errors:
            lines.append("\nErrors:")
            for e in obs.errors[:3]:
                lines.append(f"  {e}")
        return "\n".join(lines)

    def stop(self) -> None:
        self._stop.set()


def main():
    import argparse
    # Unbuffered stdout so trace writes to disk immediately
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/Users/orpington/Documents/economy-documentary-work")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()

    agent = DebugGymAgent(
        repo_root=args.repo,
        output_dir=args.output_dir,
        api_key=args.api_key,
        model=args.model,
    )

    try:
        agent.run(interval_sec=args.interval)
    except KeyboardInterrupt:
        agent.stop()
        print("\nAgent stopped by user.")


if __name__ == "__main__":
    main()
