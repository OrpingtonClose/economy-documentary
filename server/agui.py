"""
AG-UI (Agent-User Interaction) — backward-compatible re-export shim.

This module has been decomposed into four focused modules:
  - agui_events    — data models, SSE event bus, FeedbackStore, slot-state bridge
  - agui_approval  — approval gates, gatekeeper, regeneration
  - agui_slot_bridge — OTIO slot drilldown, cost preview, rewind
  - agui_endpoints — general REST endpoints (artifacts, feedback, escalation, previews)

This shim re-exports every public name so that existing imports continue to
work during the migration.  New code should import from the specific module
instead.
"""

# -- Events / data models -------------------------------------------------
from agui_events import (  # noqa: F401
    ArtifactEvent,
    ArtifactStatus,
    ArtifactType,
    FeedbackStore,
    FeedbackType,
    HumanFeedback,
    _emit_slot_state_from_artifact,
    _store,
    emit_agui_event,
    emit_otio_authoritative,
    get_feedback_store,
    subscribe_agui_events,
    unsubscribe_agui_events,
)

# -- Constants (for test monkeypatching compat) ---------------------------
from agui_events import _OUTPUT_DIR  # noqa: F401

# -- Sub-module routers ---------------------------------------------------
from agui_approval import router as _approval_router  # noqa: F401
from agui_slot_bridge import (  # noqa: F401
    api_router as _slot_api_router,
    router as _slot_bridge_router,
)
from agui_endpoints import router as _endpoints_router  # noqa: F401

# -- Combined routers (what server.py imports) ----------------------------
# The original agui.py exported two routers:
#   router     = APIRouter(prefix="/agui")  — all /agui/* endpoints
#   api_router = APIRouter(prefix="/api")   — slot drilldown + reasoning
#
# Sub-modules define their own prefix ("/agui" or "/api"), so we cannot
# include them into *another* prefixed router (that would double-prefix).
# Instead we build clean aggregation routers and include the sub-routers
# with their own prefix (which FastAPI honours as-is).

from fastapi import APIRouter

router = APIRouter(tags=["agui"])
router.include_router(_approval_router)
router.include_router(_slot_bridge_router)
router.include_router(_endpoints_router)

api_router = APIRouter(tags=["api"])
api_router.include_router(_slot_api_router)
