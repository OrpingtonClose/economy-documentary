"""Judge fleet — local-first LLM-as-judge clients, model catalog, and provisioner.

The judge layer is what lets every atomic component's eval suite grade an
agent's *judgment* rather than just its mechanical output.  Per the
long-horizon mandate ("MAKE IT CARE"), the primary judges are self-hosted
open-weight models that can look at content without refusing:

- :data:`GEMMA4_ABLITERATED` — uncensored safety / content-reject judge
  (primary).
- :data:`QWEN35_OMNI` — video+audio specialist for per-scene AV QA
  (Qwen3.5-Omni open weights; 256K context, long-form).
- :data:`VIDEO_SALMONN_2_72B` — fine-grained AV tiebreaker (ByteDance /
  Tsinghua open weights).

Proprietary APIs (Claude, GPT, Gemini) are only reachable via
:class:`FallbackJudgeClient` and must be explicitly opted-in per call —
they are never used as the primary adjudicator.

Public surface (kept deliberately small — every symbol is tested):

- :class:`JudgeClient` — abstract client protocol
- :class:`HttpJudgeClient` — talks to a self-hosted FastAPI judge server
- :class:`MockJudgeClient` — deterministic in-memory client for pytest
- :class:`JudgeRequest` / :class:`JudgeResponse` — wire contract
- :class:`JudgeModelSpec` + :data:`JUDGE_CATALOG` — hardware requirements
- :func:`fetch_model_from_b2` — pull abliterated weights from the B2 bucket
- :func:`build_judge_worker_spec` — glue into ``server/worker_provisioner.py``
"""

from __future__ import annotations

from strands_agents.judges.client import (
    HttpJudgeClient,
    JudgeClient,
    JudgeRequest,
    JudgeResponse,
    MockJudgeClient,
    build_judge_client,
)
from strands_agents.judges.fetcher import fetch_model_from_b2
from strands_agents.judges.models import (
    GEMMA4_ABLITERATED,
    JUDGE_CATALOG,
    QWEN35_OMNI,
    VIDEO_SALMONN_2_72B,
    JudgeModelSpec,
)
from strands_agents.judges.provisioner import build_judge_worker_spec

__all__ = [
    "GEMMA4_ABLITERATED",
    "HttpJudgeClient",
    "JUDGE_CATALOG",
    "JudgeClient",
    "JudgeModelSpec",
    "JudgeRequest",
    "JudgeResponse",
    "MockJudgeClient",
    "QWEN35_OMNI",
    "VIDEO_SALMONN_2_72B",
    "build_judge_client",
    "build_judge_worker_spec",
    "fetch_model_from_b2",
]
