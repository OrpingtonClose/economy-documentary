"""
Assembly Stage — deterministic final cut assembly.

Reads the OTIO timeline, muxes audio and video per scene, concatenates
all scenes into the final documentary, validates the result, and uploads
to B2.  All OTIO operations are stateless — read/write the OTIO file.

The assembly agent is a deterministic leaf: it calls assemble_final_cut
and reports completion.  No LLM creativity needed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands import Agent, tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Assembler Agent for a documentary pipeline.

Assembly is deterministic — call assemble_final_cut and report completion.
You do NOT generate content. You compose the final cut from clip artifacts
provided in your prompt.

WORKFLOW:
1. Extract clip artifacts from between the '--- CLIP ARTIFACTS ---' and
   '--- END CLIP ARTIFACTS ---' markers in your prompt.
2. Call assemble_final_cut(clip_artifacts=<the extracted JSON string>).
3. Report whether assembly succeeded or failed.

RULES:
- You NEVER read the OTIO timeline. All data arrives in your prompt.
- If clip artifacts are missing, report the error — do not attempt to generate them.
- The final output is a deliverable, not a preview.
"""


# ---------------------------------------------------------------------------
# Deterministic assembly tool
# ---------------------------------------------------------------------------


@tool
def assemble_final_cut(
    scenes: str = "",
    clip_artifacts: str = "",
    whisperx_alignment: str = "",
    timeline_path: str = "",
    output_dir: str = "",
) -> str:
    """Assemble the final documentary cut from provided clip artifacts.

    If ``clip_artifacts`` is a non-empty JSON string, it is parsed and
    passed directly to the assembler — NO OTIO read occurs.
    Otherwise falls back to reading the OTIO timeline (legacy mode).
    """
    from tools.assembly_tools import assemble_documentary
    from tools.otio_file_ops import resolve_timeline_path

    tp = timeline_path or resolve_timeline_path()
    pipeline_dir = output_dir or resolve_timeline_path().rsplit("/timelines/", 1)[0]
    out_dir = os.path.join(pipeline_dir, "output")

    parsed_artifacts = None
    if clip_artifacts and clip_artifacts.strip():
        try:
            parsed_artifacts = json.loads(clip_artifacts)
        except Exception:
            pass

    return assemble_documentary(
        timeline_path=tp,
        output_dir=out_dir,
        clip_artifacts=parsed_artifacts,
    )


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_assembly_agent(
    *,
    model: Any = None,
) -> Agent:
    """Return a configured assembly Agent with stateless OTIO tools.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``.

    Returns:
        Configured :class:`Agent` ready for the pipeline Graph.
    """
    return Agent(
        name="assembler_agent",
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        tools=[assemble_final_cut, read_timeline, validate_assembly],
    )
