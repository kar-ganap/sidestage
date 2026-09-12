"""SideStage — a live-selling copilot for eBay Live card sellers.

One process serves everything: the REST surface, the operator WebSocket, and the
static console. Run it with:

    uvicorn app.main:app --reload

See docs/DECISIONS.md D-06 and D-07 for why all logic lives server-side and why
there is no front-end build step.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import STATIC_DIR, settings

VERSION = "0.1.0"

app = FastAPI(title="SideStage", version=VERSION, docs_url="/api/docs")


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness plus enough state to debug a deploy without shell access."""
    return JSONResponse(
        {
            "ok": True,
            "version": VERSION,
            # Which side of D-17 we're running on. Never reports the key itself.
            "llm_mode": "live" if settings.use_live_llm else "replay",
            "draft_model": settings.draft_model,
            "triage_model": settings.triage_model,
            "budgets_ms": {
                "triage_fast": settings.budget_triage_fast_ms,
                "triage_escalated": settings.budget_triage_escalated_ms,
                "draft": settings.budget_draft_ms,
                "research": settings.budget_research_ms,
            },
        }
    )


# Mounted last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
