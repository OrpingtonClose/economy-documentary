#!/usr/bin/env python3
"""
Architecture contract verification entrypoint for debug-gym.

This script statically analyzes the pipeline codebase and verifies each
clause of ARCHITECTURE_CONTRACT.md. Returns exit code 0 if all clauses
pass, 1 if any critical clause fails.

Usage:
    DEBUG_GYM_REPO_ROOT=/path/to/repo python server/debug_gym_entrypoint.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


class ContractVerifier:
    """Verifies the documentary pipeline against its architecture contract."""

    def __init__(self, repo_root: str = "/testbed") -> None:
        self.repo_root = repo_root
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.fixes: list[str] = []

    def _path(self, *parts: str) -> str:
        return os.path.join(self.repo_root, *parts)

    def _fail(self, clause: str, finding: str, fix: str = "") -> None:
        self.errors.append(f"[{clause}] {finding}")
        if fix:
            self.fixes.append(f"[{clause}] {fix}")

    def _warn(self, clause: str, finding: str) -> None:
        self.warnings.append(f"[{clause}] {finding}")

    def verify_all(self) -> bool:
        print("=" * 60)
        print("ARCHITECTURE CONTRACT VERIFICATION")
        print("=" * 60)
        print(f"Repo: {self.repo_root}")
        print()

        self._verify_audio_gate()
        self._verify_resume_check()
        self._verify_tts_cache()
        self._verify_forward_edges()
        self._verify_max_nodes()
        self._verify_budget_hook()
        self._verify_vm_cleanup()
        self._verify_lock_file()
        self._verify_qa_skip()
        self._verify_master_mp4_check()
        self._verify_provisioner_dedup()

        print()
        print("=" * 60)
        print("VERIFICATION RESULTS")
        print("=" * 60)

        if self.warnings:
            print(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            for w in self.warnings:
                print(f"  {w}")

        if self.fixes:
            print(f"\n🔧 SUGGESTED FIXES ({len(self.fixes)}):")
            for f in self.fixes:
                print(f"  {f}")

        if self.errors:
            print(f"\n❌ ERRORS ({len(self.errors)}):")
            for e in self.errors:
                print(f"  {e}")
            print(f"\n❌ CONTRACT VIOLATED — {len(self.errors)} critical gap(s)")
            return False

        print("\n✅ ALL CONTRACT CLAUSES SATISFIED")
        return True

    def _verify_audio_gate(self) -> None:
        path = self._path("server", "strands_agents", "graph_pipeline.py")
        if not os.path.exists(path):
            self._fail("Stage Flow", f"graph_pipeline.py not found")
            return
        content = _read_file(path)

        # Check validate_audio checks clips first
        if "A1_Narration" not in content:
            self._fail("Stage Flow", "validate_audio missing A1_Narration check")
            return

        # Find validate_audio function
        match = re.search(r"def validate_audio\(.*?\n(?=\n    @tool|\Z)", content, re.DOTALL)
        if not match:
            self._warn("Stage Flow", "Could not isolate validate_audio function")
        else:
            func = match.group(0)
            # Should check clips before whisperx
            clip_check_pos = func.find("A1_Narration")
            whisperx_pos = func.find("WHISPERX_ALIGNMENT")
            if clip_check_pos == -1:
                self._fail("Stage Flow", "validate_audio does not check A1_Narration")
                return
            if whisperx_pos != -1 and whisperx_pos < clip_check_pos:
                self._fail("Stage Flow", "validate_audio checks whisperx before A1_Narration clips")
                return
            # Should have warnings list for whisperx
            if "warnings" not in func:
                self._warn("Stage Flow", "validate_audio may not treat whisperx as optional warning")

        print("  ✅ Audio gate: A1_Narration clips checked, whisperx optional")

    def _verify_resume_check(self) -> None:
        path = self._path("server", "strands_agents", "graph_pipeline.py")
        content = _read_file(path)

        # Find the audio agent's check_resume_status — search for it after _build_audio_agent
        audio_start = content.find("def _build_audio_agent")
        if audio_start == -1:
            self._fail("Stage Flow", "_build_audio_agent not found")
            return
        audio_section = content[audio_start:]

        # Find check_resume_status inside the audio agent
        match = re.search(r"def check_resume_status\(.*?\n(?=\n    @tool|\Z)", audio_section, re.DOTALL)
        if not match:
            self._fail("Stage Flow", "check_resume_status not found in audio agent")
            return

        func = match.group(0)
        checks = []
        if "completed_stages" in func:
            checks.append("completed_stages")
        if "otio_read" in func or "A1_Narration" in func:
            checks.append("otio_clips")
        if "glob" in func or "*.wav" in func or "wav_files" in func:
            checks.append("wav_files")

        if "otio_clips" not in checks:
            self._fail("Stage Flow", "Audio check_resume_status missing OTIO A1_Narration clip check",
                      "Add otio_read + A1_Narration track check")
        if "wav_files" not in checks:
            self._fail("Stage Flow", "Audio check_resume_status missing WAV file check",
                      "Add glob check for *.wav files")

        if len(checks) >= 2:
            print(f"  ✅ Resume check: {', '.join(checks)}")
        else:
            print(f"  ⚠️ Resume check: only {', '.join(checks)}")

    def _verify_tts_cache(self) -> None:
        path = self._path("server", "tools", "tts_tools.py")
        if not os.path.exists(path):
            self._warn("Stage Flow", "tts_tools.py not found")
            return
        content = _read_file(path)

        # Should store text in sidecar, not hash
        if "text_hash" in content and "cached_hash == text_hash" in content:
            self._fail("Stage Flow", "TTS still uses hash-based cache comparison",
                      "Store normalized text in sidecar instead of hash")
            return

        # Should compare normalized text
        if "normalized_text" in content or "text.strip()" in content:
            print("  ✅ TTS cache: text-based comparison")
        else:
            self._warn("Stage Flow", "TTS cache may not normalize text")

    def _verify_forward_edges(self) -> None:
        path = self._path("server", "strands_agents", "graph_pipeline.py")
        content = _read_file(path)

        for edge_name in ["_audio_not_completed", "_video_not_completed", "_assembly_not_completed"]:
            # Match function until next top-level def or end of file
            match = re.search(rf"def {edge_name}\(.*?(?=\ndef [a-zA-Z_]|\Z)", content, re.DOTALL)
            if not match:
                # Fallback: function might be at end of file without trailing newline
                idx = content.find(f"def {edge_name}(")
                if idx == -1:
                    self._fail("Stage Flow", f"{edge_name} not found")
                    continue
                func = content[idx:]
            else:
                func = match.group(0)
            if "otio_read" not in func and "metadata_key_exists" not in func:
                self._fail("Stage Flow", f"{edge_name} missing disk state check",
                          f"Add OTIO check to {edge_name}")
                continue

        print("  ✅ Forward edges: check disk state")

    def _verify_max_nodes(self) -> None:
        path = self._path("server", "strands_agents", "run_strands.py")
        if not os.path.exists(path):
            self._fail("Stage Flow", "run_strands.py not found")
            return
        content = _read_file(path)

        match = re.search(r'"max_nodes":\s*(\d+)', content)
        if not match:
            self._fail("Stage Flow", "max_nodes default not found")
            return

        max_nodes = int(match.group(1))
        if max_nodes < 100:
            self._fail("Stage Flow", f"max_nodes={max_nodes}, need >= 100",
                      "Change DEFAULTS['max_nodes'] to 200")
            return

        print(f"  ✅ max_nodes: {max_nodes}")

    def _verify_budget_hook(self) -> None:
        path = self._path("server", "strands_agents", "hooks", "pipeline_hooks.py")
        if not os.path.exists(path):
            self._warn("Resource Lifecycle", "pipeline_hooks.py not found")
            return
        content = _read_file(path)

        if "class BudgetHook" not in content:
            self._warn("Resource Lifecycle", "BudgetHook class not found")
            return

        # Isolate BudgetHook class body
        start = content.find("class BudgetHook")
        end = content.find("\nclass ", start + 1)
        if end == -1:
            end = len(content)
        hook_body = content[start:end]

        # Check it logs warning on budget exceeded
        if "logger.warning" not in hook_body:
            self._fail("Resource Lifecycle", "BudgetHook does not log warning on exceed")
            return

        # Check it does NOT raise or cancel nodes in on_after_node_call
        method_match = re.search(
            r"async def on_after_node_call\(self, event.*?\n(?=\n    (?:async def |@property|def )|\Z)",
            hook_body, re.DOTALL
        )
        if method_match:
            method_body = method_match.group(0)
            if "raise" in method_body:
                self._fail("Resource Lifecycle", "BudgetHook raises exception in on_after_node_call",
                          "Remove raise — log warning only")
                return
            if "cancel_node" in method_body or "event.cancel" in method_body:
                self._fail("Resource Lifecycle", "BudgetHook cancels/aborts nodes",
                          "Remove cancellation — soft limit only")
                return

        print("  ✅ BudgetHook: soft limit (warning only)")

    def _verify_vm_cleanup(self) -> None:
        path = self._path("server", "strands_agents", "run_strands.py")
        if not os.path.exists(path):
            self._fail("Resource Lifecycle", "run_strands.py not found")
            return
        content = _read_file(path)

        if "cleanup(destroy_vms=True)" not in content:
            self._fail("Resource Lifecycle", "VM cleanup not in finally block",
                      "Add provisioner.cleanup(destroy_vms=True) to finally")
            return
        if "signal.signal(signal.SIGINT" not in content:
            self._fail("Resource Lifecycle", "SIGINT handler missing",
                      "Add signal.signal(signal.SIGINT, handler) with VM cleanup")
            return

        print("  ✅ VM cleanup: finally + SIGINT")

    def _verify_lock_file(self) -> None:
        path = self._path("server", "strands_agents", "run_strands.py")
        content = _read_file(path)

        if ".pipeline.lock" not in content:
            self._fail("Concurrent Run Safety", "Lock file missing",
                      "Add .pipeline.lock acquisition")
            return
        if "os.kill" not in content:
            self._warn("Concurrent Run Safety", "Stale PID detection may be missing")

        print("  ✅ Lock file: concurrent run prevention")

    def _verify_qa_skip(self) -> None:
        path = self._path("server", "tools", "video_tools.py")
        if not os.path.exists(path):
            self._warn("QA Policy", "video_tools.py not found")
            return
        content = _read_file(path)

        if "DASHSCOPE_API_KEY" not in content:
            self._warn("QA Policy", "DASHSCOPE_API_KEY check missing")
            return

        # Find the QA section — should skip when key missing
        # Look for the pattern: if key missing -> warning, if key present -> raise
        dashscope_check = content.find("_dashscope_available")
        if dashscope_check == -1:
            dashscope_check = content.find("DASHSCOPE_API_KEY")
        qa_section = content[dashscope_check:dashscope_check + 1500]

        # Verify the logic: when NOT available, log warning (not raise)
        # The pattern should be: `and _dashscope_available:` for raise branch
        # and `and not _dashscope_available:` for skip branch
        if "not _dashscope_available" in qa_section or "not dashscope" in qa_section.lower():
            # Check that the NOT-available branch does NOT raise
            not_avail_start = qa_section.find("not _dashscope_available")
            not_avail_section = qa_section[not_avail_start:not_avail_start + 400]
            if "raise" in not_avail_section and "RuntimeError" in not_avail_section:
                self._fail("QA Policy", "QA skip branch contains raise",
                          "Remove raise from missing-key branch")
                return
        elif "_dashscope_available" in qa_section:
            # Old-style check - look for graceful skip
            pass
        else:
            self._warn("QA Policy", "Could not verify QA skip logic structure")

        print("  ✅ QA skip: graceful when key missing")

    def _verify_master_mp4_check(self) -> None:
        path = self._path("server", "strands_agents", "run_strands.py")
        content = _read_file(path)

        if "master.mp4" not in content:
            self._fail("Output Verification", "master.mp4 check missing",
                      "Add os.path.exists(master_mp4) check")
            return

        # Check that missing master.mp4 returns failed status
        if '"status": "failed"' in content:
            print("  ✅ master.mp4: verified, returns failed if missing")
        else:
            print("  ⚠️ master.mp4: check exists but may not return failed status")

    def _verify_provisioner_dedup(self) -> None:
        path = self._path("server", "worker_provisioner.py")
        if not os.path.exists(path):
            self._warn("Resource Lifecycle", "worker_provisioner.py not found")
            return
        content = _read_file(path)

        if "RLock" not in content:
            self._fail("Resource Lifecycle", "No RLock in provisioner",
                      "Use threading.RLock()")
            return
        if "MAX_TOTAL_VMS" not in content:
            self._warn("Resource Lifecycle", "MAX_TOTAL_VMS not found")
        else:
            match = re.search(r"MAX_TOTAL_VMS\s*=\s*(\d+)", content)
            if match and int(match.group(1)) > 3:
                self._fail("Resource Lifecycle", f"MAX_TOTAL_VMS={match.group(1)}, need <= 3")
                return

        print("  ✅ Provisioner: RLock + max VMs")


def main():
    repo_root = os.environ.get("DEBUG_GYM_REPO_ROOT", "/testbed")
    verifier = ContractVerifier(repo_root=repo_root)
    passed = verifier.verify_all()
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
