"""
Debug-gym environment for documentary pipeline architecture auditing.

This module provides a RepoEnv subclass that:
1. Copies the pipeline codebase into the workspace
2. Provides audit instructions to the agent
3. Evaluates compliance via debug_gym_entrypoint.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from debug_gym.gym.envs.env import EvalOutput, RepoEnv  # type: ignore[import-not-found]
from debug_gym.gym.terminals.local import LocalTerminal  # type: ignore[import-not-found]


class DocumentaryAuditEnv(RepoEnv):
    """Debug-gym environment for auditing the documentary pipeline architecture."""

    def __init__(
        self,
        task_data: dict | None = None,
        *,
        terminal=None,
        **kwargs: Any,
    ) -> None:
        terminal = terminal or LocalTerminal(logger=kwargs.get("logger"))
        task_data = task_data or {}
        super().__init__(
            task_data=task_data,
            terminal=terminal,
            **kwargs,
        )

    @property
    def task_name(self):
        return "documentary_pipeline_audit"

    def setup_task(self) -> None:
        self.terminal.task_name = self.task_name

    def setup_workspace(self):
        """Copy pipeline codebase into workspace."""
        self.workspace.reset()

        # Copy the project root into workspace
        project_root = Path(__file__).parent.parent
        if project_root.exists():
            self.workspace.copy_content(
                src=str(project_root),
                target=self.workspace.working_dir,
            )
            self.logger.info(f"Copied pipeline codebase to {self.workspace.working_dir}")

    def setup_terminal(self) -> None:
        """Set up git for patch tracking."""
        self.logger.debug(f"Configuring {self.terminal}...")
        self.terminal.run("git init -b main", raises=False)
        self.terminal.run("git config user.name 'audit-agent'", raises=False)
        self.terminal.run("git config user.email '<>'", raises=False)
        self.terminal.run("git add *", raises=False)
        self.terminal.run("git commit -am 'Initial commit'", raises=False)

    @property
    def instructions(self) -> str:
        return (
            "You are auditing a documentary pipeline codebase against its architecture contract.\n\n"
            "The pipeline is at /testbed/server/ and should satisfy these hard contracts:\n"
            "1. Stage Flow: SCENARIO → AUDIO → VIDEO → ASSEMBLY (each exactly once, no loops)\n"
            "2. Audio gate passes if A1_Narration clips exist (whisperx_alignment optional)\n"
            "3. check_resume_status checks disk state (OTIO clips + WAV files), not just completed_stages\n"
            "4. TTS cache stores normalized text, not hash\n"
            "5. Forward edge conditions check actual OTIO disk state\n"
            "6. max_nodes default >= 100\n"
            "7. BudgetHook is soft limit (warning only, no abort)\n"
            "8. VM cleanup in finally block + SIGINT handler\n"
            "9. Lock file prevents concurrent runs with stale PID detection\n"
            "10. QA skips gracefully when DASHSCOPE_API_KEY missing\n"
            "11. master.mp4 verified before returning 'completed'\n"
            "12. WorkerProvisioner uses RLock, max 3 VMs\n\n"
            "WORKFLOW:\n"
            "1. Read ARCHITECTURE_CONTRACT.md\n"
            "2. Inspect relevant source files (graph_pipeline.py, run_strands.py, provisioner_agent.py, vm_registry.py)\n"
            "3. For each clause: verify the code satisfies it\n"
            "4. If a clause is violated, fix it with bash/sed\n"
            "5. When all clauses pass, call submit\n\n"
            "The eval will run debug_gym_entrypoint.py to verify all clauses."
        )

    def set_entrypoints(self, entrypoint: str, debug_entrypoint: str | None = None) -> None:
        """Set the entrypoint to the contract verification script."""
        self.entrypoint = f"cd {self.workspace.working_dir} && python server/debug_gym_entrypoint.py"
        self.debug_entrypoint = debug_entrypoint

    def eval(self, **kwargs) -> EvalOutput:
        """Run the contract verification entrypoint."""
        if not self.entrypoint:
            self.set_entrypoints("")
        success, output = self.terminal.run(self.entrypoint, timeout=self.run_timeout)
        self.last_eval = EvalOutput(success, output)
        return self.last_eval
