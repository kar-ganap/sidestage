"""SideStage — a live-selling copilot for eBay Live card sellers.

One process serves everything: the REST surface, the operator console, and the
static files. Run it with:

    uv run uvicorn app.main:app --reload

See docs/DECISIONS.md D-06 and D-07 for why all logic lives server-side and why
there is no front-end build step.

THE API IS THE WORKFLOW, in the order a reviewer should exercise it:

    POST /api/chat            one message in -> triage decides, with reasons
    POST /api/replay          push the real observed transcript through it
    GET  /api/state           chat log, queue, ledger, counters
    POST /api/cards/{id}/draft   evidence -> generate -> verify -> repair
    POST /api/cards/{id}/send    the operator's decision, journalled

Dropped messages come back from `/api/state` alongside surfaced ones, carrying
the features that dropped them. That is the product, not a debug view.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import STATIC_DIR, settings
from app.models import Intent
from app.session import Card, LoggedMessage, get_session, reset_session
from app.triage import Model

VERSION = "0.2.0"

app = FastAPI(title="SideStage", version=VERSION, docs_url="/api/docs")


# =====================================================================
# Serialisation
# =====================================================================


def _msg(m: LoggedMessage) -> dict:
    d = asdict(m)
    d["at"] = m.at.isoformat(timespec="seconds")
    d["route"] = m.route.value
    d["intent"] = m.intent.value
    d["surfaced"] = m.surfaced
    d["score"] = round(m.score, 3)
    return d


def _card(c: Card) -> dict:
    d = asdict(c)
    d["at"] = c.at.isoformat(timespec="seconds")
    d["intent"] = c.intent.value
    return d


def _lot(lot) -> dict | None:
    if lot is None:
        return None
    return {"id": lot.id, "title": lot.title, "format": lot.format.value,
            "status": lot.status, "price": lot.price,
            "current_bid": lot.current_bid, "quantity": lot.quantity}


# =====================================================================
# Read
# =====================================================================


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness plus enough state to debug a deploy without shell access."""
    m = Model.load()
    return JSONResponse({
        "ok": True,
        "version": VERSION,
        # Which side of D-17 we're running on. Never reports the key itself.
        "llm_mode": "live" if settings.use_live_llm else "replay",
        "draft_model": settings.draft_model,
        "triage_model": settings.triage_model,
        "draft_thinking": settings.draft_thinking,
        "triage_model_fitted": m.fitted_on or "unfitted",
        "triage_threshold": m.threshold,
        "budgets_ms": {
            "triage_fast": settings.budget_triage_fast_ms,
            "triage_escalated": settings.budget_triage_escalated_ms,
            "draft": settings.budget_draft_ms,
            "research": settings.budget_research_ms,
        },
    })


@app.get("/api/state")
def state() -> JSONResponse:
    s = get_session()
    return JSONResponse({
        "lot": _lot(s.active_lot),
        "nudge": s.nudge(),
        "lots": [_lot(l) for l in s.catalog.lots.values()],
        "log": [_msg(m) for m in reversed(s.log)],
        "queue": [_card(c) for c in s.queue()],
        "ledger": list(reversed(s.ledger)),
        "stats": s.stats(),
    })


# =====================================================================
# Write
# =====================================================================


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.post("/api/chat")
def chat(body: ChatIn) -> JSONResponse:
    """One message through the cascade. Cheap — no model call unless the gate
    passes it, which on real traffic is ~35%."""
    return JSONResponse(_msg(get_session().ingest(body.text)))


class ReplayIn(BaseModel):
    n: int = Field(default=40, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    source: str = Field(default="triage_test")


@app.post("/api/replay")
def replay(body: ReplayIn) -> JSONResponse:
    """Push the real observed transcript through the live cascade.

    This is the honest demo: not messages written to make the system look good,
    but the actual chat from the recorded show — including the 84% that is noise
    and the handful the platform's own highlighter missed.
    """
    import json
    from pathlib import Path
    path = Path(__file__).parent.parent / "evals" / "data" / f"{body.source}.jsonl"
    if not path.exists():
        return JSONResponse({"error": f"no transcript {body.source}"}, status_code=404)
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if "_meta" not in r][body.offset:body.offset + body.n]
    s = get_session()
    out = [_msg(s.ingest(r["text"])) for r in rows]
    return JSONResponse({"ingested": len(out), "messages": out,
                         "stats": s.stats()})


@app.post("/api/cards/{card_id}/draft")
def draft(card_id: str) -> JSONResponse:
    """Evidence -> generate -> verify -> at most one repair (app/pipeline.py).

    Blocking, and it takes seconds. The console shows a pending state rather
    than pretending otherwise — B-02's lesson is that a status which cannot
    report its real state is worse than none.
    """
    card = get_session().draft(card_id)
    if card is None:
        return JSONResponse({"error": "no such card"}, status_code=404)
    return JSONResponse(_card(card))


@app.get("/api/cards/{card_id}/judgement")
def judgement(card_id: str) -> JSONResponse:
    """The second opinion, if it has landed. Never blocks (B-32)."""
    op = get_session().judgement(card_id)
    if op is None:
        return JSONResponse({"pending": True})
    return JSONResponse({"pending": False, "responsive": op.responsive,
                         "why": op.why, "latency_ms": op.latency_ms,
                         "model": op.model, "errored": op.errored})


class SendIn(BaseModel):
    text: str | None = None


@app.post("/api/cards/{card_id}/send")
def send(card_id: str, body: SendIn | None = None) -> JSONResponse:
    entry = get_session().send(card_id, text=(body.text if body else None))
    if entry is None:
        return JSONResponse({"error": "no such card"}, status_code=404)
    return JSONResponse(entry)


@app.post("/api/cards/{card_id}/dismiss")
def dismiss(card_id: str) -> JSONResponse:
    ok = get_session().dismiss(card_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/lot/{lot_id}")
def set_lot(lot_id: str) -> JSONResponse:
    ok = get_session().set_active_lot(lot_id)
    return JSONResponse({"ok": ok, "lot": _lot(get_session().active_lot)},
                        status_code=200 if ok else 404)


@app.post("/api/reset")
def reset() -> JSONResponse:
    reset_session()
    return JSONResponse({"ok": True})


@app.get("/api/intents")
def intents() -> JSONResponse:
    return JSONResponse({"intents": [i.value for i in Intent]})


# Mounted last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
