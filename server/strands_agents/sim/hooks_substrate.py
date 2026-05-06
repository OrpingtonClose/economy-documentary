"""
Substrate as HookProvider — converts the 6 test fakes into Strands hooks.

The old Substrate class used ``set_*_helpers`` injection slots on each
module. The Strands equivalent registers each fake as a
:class:`HookProvider` that intercepts the right lifecycle events:
  - FakeLLM → BeforeModelCallEvent (scripted responses)
  - FakeTTS → BeforeToolCallEvent (TTS tools)
  - FakeRenderer → BeforeToolCallEvent (GPU dispatch tools)
  - FakeB2 → BeforeToolCallEvent (B2 upload tools)
  - FakeClock → BeforeInvocationEvent (time injection)
  - FakeInterrupt → BeforeNodeCallEvent (interrupt simulation)

The :class:`HookSubstrate` owns all six fakes plus a shared
:class:`Recorder`, and knows how to register them on a
:class:`HookRegistry`. The context manager ``with substrate.hooks_installed(registry)``
guarantees cleanup.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from strands.hooks import (
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    BeforeNodeCallEvent,
    HookProvider,
    HookRegistry,
)

from strands_agents.sim.b2 import FakeB2
from strands_agents.sim.clock import FakeClock
from strands_agents.sim.interrupt import FakeInterrupt
from strands_agents.sim.llm import FakeLLM, LLMScript
from strands_agents.sim.recorder import Recorder
from strands_agents.sim.renderer import FakeRenderer
from strands_agents.sim.tts import FakeTTS
from strands_agents.tools.task_pool import AsyncTaskPool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual hook providers
# ---------------------------------------------------------------------------


class LLMHookProvider(HookProvider):
    """Intercepts model calls and returns scripted responses."""

    def __init__(self, fake_llm: FakeLLM) -> None:
        self._llm = fake_llm

    async def on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        """Return a scripted response instead of calling the real model."""
        # The FakeLLM has a script that maps prompts to responses
        response = self._llm.get_response(event)
        if response is not None:
            event.stop_response = response


class TTSHookProvider(HookProvider):
    """Intercepts TTS tool calls with fake audio generation."""

    def __init__(self, fake_tts: FakeTTS) -> None:
        self._tts = fake_tts

    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_name in ("tts_generate", "launch_audio_render"):
            result = self._tts.tts_generate(event)
            event.cancel_tool(result=result)


class RendererHookProvider(HookProvider):
    """Intercepts GPU render tool calls with fake video generation."""

    def __init__(self, fake_renderer: FakeRenderer) -> None:
        self._renderer = fake_renderer

    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_name in ("launch_visual_production", "render_video"):
            result = self._renderer.dispatch(event)
            event.cancel_tool(result=result)


class B2HookProvider(HookProvider):
    """Intercepts B2 upload tool calls with fake uploads."""

    def __init__(self, fake_b2: FakeB2) -> None:
        self._b2 = fake_b2

    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_name in ("upload_to_b2", "b2_upload", "launch_b2_sync"):
            result = self._b2.upload(event)
            event.cancel_tool(result=result)


class ClockHookProvider(HookProvider):
    """Injects synthetic time into the pipeline."""

    def __init__(self, fake_clock: FakeClock) -> None:
        self._clock = fake_clock

    async def on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        """Inject current time from the fake clock."""
        # The FakeClock provides a controllable time source
        self._clock.tick(event)


class InterruptHookProvider(HookProvider):
    """Simulates interrupts for approval gate testing."""

    def __init__(self, fake_interrupt: FakeInterrupt) -> None:
        self._interrupt = fake_interrupt

    async def on_before_node_call(self, event: BeforeNodeCallEvent) -> None:
        """Simulate an interrupt on specific nodes."""
        if self._interrupt.should_interrupt(event):
            event.interrupt()


# ---------------------------------------------------------------------------
# HookSubstrate — the composition layer
# ---------------------------------------------------------------------------


class HookSubstrate:
    """All six fakes as Strands HookProviders, ready to register.

    Usage::

        substrate = HookSubstrate(llm_script=my_script)
        with substrate.hooks_installed(registry):
            # All fakes are active; real services are blocked
            result = await graph.invoke_async("Make a documentary")

        # Fakes are unregistered; real services restored
    """

    def __init__(
        self,
        *,
        llm_script: LLMScript | None = None,
        workers_total: int = 2,
    ) -> None:
        self.recorder = Recorder()
        self.clock = FakeClock(recorder=self.recorder)
        self.b2 = FakeB2(recorder=self.recorder)
        self.llm = FakeLLM(llm_script, recorder=self.recorder)
        self.tts = FakeTTS(recorder=self.recorder)
        self.renderer = FakeRenderer(
            recorder=self.recorder, workers_total=workers_total
        )
        self.interrupt = FakeInterrupt(recorder=self.recorder)
        self.pool = AsyncTaskPool(max_workers=workers_total)

        # Build hook providers
        self._hooks = [
            LLMHookProvider(self.llm),
            TTSHookProvider(self.tts),
            RendererHookProvider(self.renderer),
            B2HookProvider(self.b2),
            ClockHookProvider(self.clock),
            InterruptHookProvider(self.interrupt),
        ]
        self._registered = False

    def register(self, registry: HookRegistry) -> None:
        """Register all hook providers on a HookRegistry."""
        if self._registered:
            return
        for hook in self._hooks:
            registry.register(hook)
        self._registered = True

    def unregister(self, registry: HookRegistry) -> None:
        """Unregister all hook providers from a HookRegistry."""
        if not self._registered:
            return
        for hook in self._hooks:
            registry.unregister(hook)
        self._registered = False

    @contextmanager
    def hooks_installed(self, registry: HookRegistry) -> Iterator[HookSubstrate]:
        """Install hooks on entry, remove on exit."""
        self.register(registry)
        try:
            yield self
        finally:
            self.unregister(registry)

    def shutdown(self) -> None:
        """Release the AsyncTaskPool."""
        self.pool.shutdown(wait=False)
