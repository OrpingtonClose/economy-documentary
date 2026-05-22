#!/usr/bin/env python3
"""
Pipeline runner with real-time architecture auditing.

This script wraps the documentary pipeline execution with an automated
audit that compares execution against the architecture contract in real
time. If critical gaps are detected (stage loops, missing outputs, VM
leaks), the script can abort the pipeline to prevent credit burn.

Usage:
    python scripts/run_pipeline_with_audit.py "The invention of the lightbulb" \
        --api-key $DEEPSEEK_API_KEY --budget 10

Architecture:
    1. Start pipeline as a subprocess
    2. Tail the log file in a background thread
    3. Parse events (stage transitions, audio generations, video renders, errors)
    4. Compare against ARCHITECTURE_CONTRACT.md clauses
    5. If critical gaps detected → kill pipeline, destroy VMs, report gaps
    6. If pipeline completes → run full audit, report result
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Gap:
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    clause: str
    finding: str
    fix: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExecutionMetrics:
    stage_order: list[tuple[str, str]] = field(default_factory=list)
    audio_generations: int = 0
    video_renders: int = 0
    vm_events: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    start_time: float = 0.0
    gaps: list[Gap] = field(default_factory=list)

    def has_loop(self) -> bool:
        """Detect if audio restarted after video finalized.
        
        Requires at least 2 audio generations after video finalized
        to avoid false positives from the first audio stage finishing late.
        """
        video_finalized = False
        post_video_audio = 0
        for stage, _ in self.stage_order:
            if stage == "video_finalized":
                video_finalized = True
            elif video_finalized and stage == "audio":
                post_video_audio += 1
        return post_video_audio >= 2

    def get_max_stages(self) -> int:
        """Count how many times each stage ran."""
        counts = {}
        for stage, _ in self.stage_order:
            if stage not in ("video_finalized", "audio_reloop"):
                counts[stage] = counts.get(stage, 0) + 1
        return max(counts.values()) if counts else 0


class PipelineAuditor:
    """Real-time pipeline execution auditor."""

    STOP_TRIGGERS = {
        "audio_loop": "Audio restarted after video completed",
        "stage_loop": "Any stage ran more than 3 times",
        "no_output": "Pipeline ran >90 min with no master.mp4",
        "vm_leak": "More than 3 VMs provisioned",
    }

    def __init__(self, output_dir: str, budget: float = 15.0):
        self.output_dir = output_dir
        self.budget = budget
        self.log_path = os.path.join(output_dir, "pipeline_live.log")
        self.metrics = ExecutionMetrics()
        self._stop_requested = False
        self._proc: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None

    def start(self, proc: subprocess.Popen) -> None:
        """Start monitoring a pipeline process."""
        self._proc = proc
        self.metrics.start_time = time.time()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        """Background thread: tail log and detect gaps."""
        # Wait for log file to exist
        for _ in range(60):
            if os.path.exists(self.log_path):
                break
            time.sleep(1)

        if not os.path.exists(self.log_path):
            self.metrics.gaps.append(Gap(
                severity="CRITICAL",
                clause="Logging: pipeline log must be created",
                finding="Log file never appeared",
            ))
            return

        with open(self.log_path, "r") as f:
            # Seek to end and tail new lines
            f.seek(0, 2)
            while self._proc and self._proc.poll() is None and not self._stop_requested:
                line = f.readline()
                if not line:
                    time.sleep(2)
                    continue
                self._parse_line(line.strip())
                self._check_stop_conditions()

            # Drain remaining lines
            for line in f:
                self._parse_line(line.strip())

        self._final_audit()

    def _parse_line(self, line: str) -> None:
        """Parse a single log line for events."""
        if "Generated narration WAV" in line:
            self.metrics.audio_generations += 1
            self.metrics.stage_order.append(("audio", line))
        elif "Rendered clip" in line or "Generated video clip" in line:
            self.metrics.video_renders += 1
            self.metrics.stage_order.append(("video", line))
        elif "production finalized" in line:
            self.metrics.stage_order.append(("video_finalized", line))
        elif "Text changed for" in line:
            self.metrics.stage_order.append(("audio_reloop", line))
        elif "Registered owned VM" in line or "VM provisioned" in line:
            # Only count actual VM creation events, not status checks
            self.metrics.vm_events.append({"line": line, "time": time.time()})
        elif "failed" in line.lower() or "error" in line.lower() or "exception" in line.lower():
            self.metrics.errors.append(line)

    def _check_stop_conditions(self) -> None:
        """Check if any stop trigger fired."""
        elapsed = time.time() - self.metrics.start_time

        # 1. Audio loop after video finalized
        if self.metrics.has_loop():
            self._request_stop("audio_loop")
            return

        # 2. Stage ran too many times
        if self.metrics.get_max_stages() > 3:
            self._request_stop("stage_loop")
            return

        # 2b. Stuck provisioning — no VM events AND no log growth for 20 min
        # Model download can take 10-15 min legitimately; only abort if truly dead.
        if elapsed > 1200 and not self.metrics.vm_events:
            # Check if log file is still growing (indicates active work)
            try:
                log_size = os.path.getsize(self.log_path)
                time.sleep(1)
                log_size_now = os.path.getsize(self.log_path)
                if log_size_now > log_size:
                    # Log is growing — pipeline is active, don't abort
                    pass
                else:
                    self.metrics.gaps.append(Gap(
                        severity="CRITICAL",
                        clause="VM Provisioning: worker must boot within 20 min",
                        finding=f"No VM provisioned after {elapsed/60:.1f} min and log is stale",
                        fix="Reduce provisioner timeout or check Vast.ai API",
                    ))
                    self._request_stop("provisioning_stuck")
                    return
            except Exception as exc:
                logger.error("Provisioning check failed: %s", exc)
                self.metrics.gaps.append(Gap(
                    severity="CRITICAL",
                    clause="VM Provisioning: worker must boot within 20 min",
                    finding=f"No VM provisioned after {elapsed/60:.1f} min (check failed: {exc})",
                    fix="Reduce provisioner timeout or check Vast.ai API",
                ))
                self._request_stop("provisioning_stuck")
                return

        # 3. No output after 90 minutes
        master_path = os.path.join(self.output_dir, "master.mp4")
        if elapsed > 5400 and not os.path.exists(master_path):  # 90 min
            self._request_stop("no_output")
            return

        # 4. VM leak — count unique instance IDs
        unique_vms = set()
        for ev in self.metrics.vm_events:
            line = ev["line"]
            # Extract instance ID from "Registered owned VM: 37070008" or "VM provisioned: instance_id=37070008"
            import re
            m = re.search(r"instance_id[=:]\s*(\d+)", line)
            if m:
                unique_vms.add(m.group(1))
            else:
                m2 = re.search(r"VM:\s*(\d+)", line)
                if m2:
                    unique_vms.add(m2.group(1))
        if len(unique_vms) > 3:
            self._request_stop("vm_leak")
            return

    def _request_stop(self, trigger: str) -> None:
        """Request pipeline stop and record the gap."""
        if self._stop_requested:
            return
        self._stop_requested = True

        reason = self.STOP_TRIGGERS.get(trigger, trigger)
        self.metrics.gaps.append(Gap(
            severity="CRITICAL",
            clause=f"STOP trigger: {reason}",
            finding=f"Trigger '{trigger}' fired after {self.metrics.get_max_stages()} stage iterations",
            fix="Abort pipeline to prevent credit burn",
        ))

        print(f"\n[AUDIT] CRITICAL: {reason}")
        print("[AUDIT] Requesting pipeline stop...")

        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()

        # Destroy VMs
        try:
            import worker_provisioner
            prov = worker_provisioner.get_provisioner()
            if prov:
                print("[AUDIT] Destroying VMs...")
                prov.cleanup(destroy_vms=True)
        except Exception as e:
            print(f"[AUDIT] VM cleanup error: {e}")

    def _final_audit(self) -> None:
        """Run final audit after pipeline exits."""
        elapsed = time.time() - self.metrics.start_time
        master_path = os.path.join(self.output_dir, "master.mp4")

        # Check 1: Output exists
        if not os.path.exists(master_path):
            self.metrics.gaps.append(Gap(
                severity="CRITICAL",
                clause="Output Verification: master.mp4 must exist",
                finding="master.mp4 was not produced",
                fix="Check stage flow and ensure assembly runs",
            ))

        # Check 2: Stage flow (each stage exactly once)
        stage_counts = {}
        for stage, _ in self.metrics.stage_order:
            if stage not in ("video_finalized", "audio_reloop"):
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
        for stage, count in stage_counts.items():
            if count > 1:
                self.metrics.gaps.append(Gap(
                    severity="CRITICAL",
                    clause="Stage Flow: each stage runs exactly once",
                    finding=f"Stage '{stage}' ran {count} times",
                    fix="Fix gate validation or backward edge routing",
                ))

        # Check 3: Duration
        if elapsed > 3600:  # 1 hour
            self.metrics.gaps.append(Gap(
                severity="HIGH",
                clause="Duration: expected 15-30 min",
                finding=f"Pipeline ran for {elapsed/60:.1f} min",
                fix="Optimize slow stages or add timeouts",
            ))

        # Check 4: VM count — unique instance IDs
        unique_vms = set()
        for ev in self.metrics.vm_events:
            line = ev["line"]
            m = re.search(r"instance_id[=:]\s*(\d+)", line)
            if m:
                unique_vms.add(m.group(1))
            else:
                m2 = re.search(r"VM:\s*(\d+)", line)
                if m2:
                    unique_vms.add(m2.group(1))
        if len(unique_vms) > 3:
            self.metrics.gaps.append(Gap(
                severity="CRITICAL",
                clause="VM Provisioning: max 3 VMs total",
                finding=f"Detected {len(unique_vms)} unique VMs: {sorted(unique_vms)}",
                fix="Fix provisioner deduplication logic",
            ))

    def report(self) -> dict[str, Any]:
        """Generate audit report."""
        elapsed = time.time() - self.metrics.start_time
        master_path = os.path.join(self.output_dir, "master.mp4")

        return {
            "status": "failed" if self.metrics.gaps else "passed",
            "duration_min": round(elapsed / 60, 1),
            "master_mp4_exists": os.path.exists(master_path),
            "audio_generations": self.metrics.audio_generations,
            "video_renders": self.metrics.video_renders,
            "vm_events": len(self.metrics.vm_events),
            "errors": len(self.metrics.errors),
            "gaps": [
                {
                    "severity": g.severity,
                    "clause": g.clause,
                    "finding": g.finding,
                    "fix": g.fix,
                }
                for g in self.metrics.gaps
            ],
            "gap_count": len(self.metrics.gaps),
            "critical_gaps": sum(1 for g in self.metrics.gaps if g.severity == "CRITICAL"),
        }


def main():
    parser = argparse.ArgumentParser(description="Run documentary pipeline with real-time audit")
    parser.add_argument("brief", nargs="+", help="Documentary brief")
    parser.add_argument("--api-key", "-k", required=True, help="LLM API key")
    parser.add_argument("--model", "-m", default="deepseek-chat", help="Model ID")
    parser.add_argument("--budget", "-b", type=float, default=15.0, help="Budget USD")
    parser.add_argument("--output-dir", "-o", default="/tmp/documentary-pipeline", help="Output directory")
    parser.add_argument("--max-nodes", type=int, default=200, help="Max node executions")
    parser.add_argument("--no-audit-abort", action="store_true", help="Don't abort on audit gaps (warn only)")

    args = parser.parse_args()

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    # Ensure log file exists before pipeline starts
    log_path = os.path.join(output_dir, "pipeline_live.log")
    with open(log_path, "w") as f:
        f.write(f"[AUDIT] Pipeline audit started at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    brief = " ".join(args.brief)

    # Find the correct Python interpreter for the pipeline.
    # The pipeline needs opentimelineio which may not be in the current venv.
    python_exe = sys.executable
    try:
        test = subprocess.run(
            [python_exe, "-c", "import opentimelineio"],
            capture_output=True, timeout=5,
        )
        if test.returncode != 0:
            # Fallback: hardcoded paths known to have opentimelineio
            for candidate in [
                "/opt/homebrew/opt/python@3.11/bin/python3.11",
                "/usr/bin/python3",
                "python3",
                "python",
            ]:
                try:
                    test2 = subprocess.run(
                        [candidate, "-c", "import opentimelineio"],
                        capture_output=True, timeout=5,
                    )
                    if test2.returncode == 0:
                        python_exe = candidate
                        break
                except Exception as exc:
                    logger.debug("Python candidate %s rejected: %s", candidate, exc)
    except Exception as exc:
        logger.error("Python exe detection failed: %s", exc)

    # Kill orphan processes from previous runs on same output dir
    import signal
    result = subprocess.run(
        ["pgrep", "-f", f"run_strands.*--output-dir {output_dir}"],
        capture_output=True, text=True,
    )
    for orphan in result.stdout.strip().split("\n"):
        if orphan.strip():
            try:
                os.kill(int(orphan.strip()), signal.SIGTERM)
                print(f"[AUDIT] Killed orphan process: {orphan.strip()}")
            except Exception as exc:
                logger.warning("Failed to kill orphan %s: %s", orphan.strip(), exc)

    cmd = [
        python_exe, "-m", "strands_agents.run_strands",
        brief,
        "--api-key", args.api_key,
        "--model", args.model,
        "--budget", str(args.budget),
        "--max-nodes", str(args.max_nodes),
        "--output-dir", output_dir,
    ]

    print("=" * 60)
    print("PIPELINE AUDIT RUNNER")
    print("=" * 60)
    print(f"Brief: {brief[:60]}")
    print(f"Budget: ${args.budget:.2f}")
    print(f"Max nodes: {args.max_nodes}")
    print(f"Output: {output_dir}")
    print(f"Audit abort: {'disabled' if args.no_audit_abort else 'enabled'}")
    print("=" * 60)

    auditor = PipelineAuditor(output_dir=output_dir, budget=args.budget)

    # Start pipeline
    env = os.environ.copy()
    env["PIPELINE_DIR"] = output_dir
    proc = subprocess.Popen(
        cmd,
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
        cwd=os.path.join(os.path.dirname(__file__), "..", "server"),
        env=env,
    )

    print(f"\nPipeline started (pid={proc.pid})")
    auditor.start(proc)

    # Wait for completion
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[INTERRUPT] Ctrl+C — terminating pipeline...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Wait for monitor to finish
    if auditor._monitor_thread:
        auditor._monitor_thread.join(timeout=10)

    # Final report
    report = auditor.report()

    print("\n" + "=" * 60)
    print("AUDIT REPORT")
    print("=" * 60)
    print(f"Status: {'✅ PASSED' if report['status'] == 'passed' else '❌ FAILED'}")
    print(f"Duration: {report['duration_min']} min")
    print(f"master.mp4: {'✅' if report['master_mp4_exists'] else '❌'}")
    print(f"Audio gens: {report['audio_generations']}")
    print(f"Video renders: {report['video_renders']}")
    print(f"VM events: {report['vm_events']}")
    print(f"Errors: {report['errors']}")
    print(f"Gaps: {report['gap_count']} ({report['critical_gaps']} critical)")

    if report["gaps"]:
        print("\nGaps:")
        for gap in report["gaps"]:
            print(f"  [{gap['severity']}] {gap['clause']}")
            print(f"    {gap['finding']}")
            if gap["fix"]:
                print(f"    Fix: {gap['fix']}")

    print("=" * 60)

    # Write JSON report
    report_path = os.path.join(output_dir, "audit_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved: {report_path}")

    sys.exit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
