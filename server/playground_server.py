"""Minimal FastAPI entrypoint for the Component Playground staging VM.

``server.server:app`` mounts the full ADK pipeline (dashboard, agui,
fleet, agents, google-adk, ag-ui) — none of which the playground needs.
This module exposes only the ``/playground`` router plus a ``/health``
check so the sealed inspection workbench can run on a CPU-only VM
without dragging in GPU/TTS/B2/dashboard dependencies.

Used by ``scripts/playground_staging_bootstrap.sh`` — the supervisor
config points uvicorn at ``playground_server:app``.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

app = FastAPI(
    title="Component Playground",
    description="Sealed inspection workbench for the 15 atomic components.",
    version="0.1.0",
)

# The frontend proxies requests through Next.js' rewrites, so in-cluster
# traffic never hits CORS. Kept permissive for anyone poking at the
# backend directly (curl, browser devtools) against the public IP.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Cheap liveness probe for nginx / supervisor / smoke tests."""
    return {"status": "ok", "service": "component-playground"}


# Lazy import so the module file itself parses even if optional deps
# aren't installed — lets the supervisor log a clean import error
# rather than a silent restart loop on a deps-drift bug.
from playground import router as playground_router  # noqa: E402

app.include_router(playground_router)


# Handy for `python -m playground_server` one-shots during debugging.
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "playground_server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PLAYGROUND_PORT", "8000")),
        reload=False,
    )
