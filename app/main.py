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
    POST /api/actions            a write, with read-back and a recorded inverse

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
from app.actions.adapter import AdapterError
from app.actions.ledger import LedgerError
from app.session import Card, EmptyReply, LoggedMessage, get_session, reset_session
from app.triage import Model

VERSION = "0.2.0"

# B-40. `/api/replay` used to interpolate the caller's string into a path:
#
#     evals/data/{body.source}.jsonl
#
# which reads any .jsonl on the filesystem given enough `../`, and 500s with a
# KeyError on anything that parses but has no "text" field. An allowlist built
# by *listing the directory* is the fix rather than a `..` check, because it can
# only ever name files that are actually there — there is no string to sanitise.
_TRANSCRIPTS: dict[str, "Path"] = {}


def _transcripts() -> dict[str, "Path"]:
    from pathlib import Path
    d = Path(__file__).parent.parent / "evals" / "data"
    return {p.stem: p for p in sorted(d.glob("*.jsonl"))}

app = FastAPI(title="SideStage", version=VERSION, docs_url="/api/docs")
_TRANSCRIPTS.update(_transcripts())


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
    """Everything the console renders, from one consistent read.

    The log and the ledger come from `snapshot()` rather than the live
    containers: this endpoint is polled while the cascade is ingesting, and
    iterating a container another thread is appending to raised a 500 on ~28%
    of concurrent reads (B-38).
    """
    s = get_session()
    log, ledger = s.snapshot()
    return JSONResponse({
        "lot": _lot(s.active_lot),
        "nudge": s.nudge(),
        "lots": [_lot(l) for l in s.catalog.lots.values()],
        "log": [_msg(m) for m in reversed(log)],
        "queue": [_card(c) for c in s.queue()],
        "ledger": list(reversed(ledger)),
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
    path = _TRANSCRIPTS.get(body.source)
    if path is None:
        return JSONResponse(
            {"error": f"no transcript {body.source!r}",
             "available": sorted(_TRANSCRIPTS)}, status_code=404)
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
    try:
        entry = get_session().send(card_id, text=(body.text if body else None))
    except EmptyReply:
        # 409, not 404 and not 500: the card exists and the request was
        # well-formed, but there is nothing to send. Journalling it anyway
        # would put a line in the ledger claiming a reply the buyer never got.
        return JSONResponse(
            {"error": "nothing to send — the draft is empty; "
                      "redraft it or type a reply"}, status_code=409)
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


class ActionIn(BaseModel):
    action: str = Field(pattern="^(push_lot|swap_showcase|markdown|adjust_quantity)$")
    params: dict = Field(default_factory=dict)


@app.post("/api/actions")
def act(body: ActionIn) -> JSONResponse:
    """The write path: propose -> confirm -> execute -> read back -> journal.

    B-73. This route is why `app/actions/` exists in the product rather than
    only in its tests. Until it was added, 53 KB of adapter and ledger — the
    idempotency key, the read-back, the recorded inverse, six marketplace fault
    modes — was imported by nothing but its own 46 tests. A concrete failure
    path that is not on any path is a claim, not a property.

    Run the server with `SIDESTAGE_FAULTS=1` and the same call survives a lost
    response, a rate limit and a stale read, because every attempt reuses the
    key minted at propose time.
    """
    try:
        return JSONResponse(get_session().act(body.action, body.params))
    except LedgerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (KeyError, TypeError) as exc:
        # A missing `lot_id` is the operator's request being wrong, not ours.
        return JSONResponse(
            {"error": f"{body.action} needs different params: {exc}"},
            status_code=422)


@app.get("/api/actions/lots")
def action_lots() -> JSONResponse:
    """The MARKETPLACE's copy of every lot, not ours.

    Kept separate from `/api/state` on purpose: the whole reason a read-back can
    disagree is that the marketplace holds its own row with its own version
    counter. Showing both is what makes a divergence legible instead of
    mysterious.
    """
    a = get_session().actions.adapter
    out = []
    for lot_id in sorted(a.lot_ids()):
        try:
            v = a.read_lot(lot_id)
        except AdapterError as exc:
            # A read is allowed to fail: with faults on, this route hits the
            # same 503s and rate limits the write path does. Reporting the lot
            # as unreadable is the honest render — dropping it would make the
            # view silently incomplete, and a 500 would make one flaky lot take
            # down the whole panel.
            out.append({"lot_id": lot_id, "unreadable": type(exc).__name__})
            continue
        out.append({"lot_id": lot_id, "status": v.status, "price": v.price,
                    "quantity": v.quantity, "position": v.position,
                    "version": v.version})
    return JSONResponse({"lots": out, "adapter_stats": a.stats})


class BidIn(BaseModel):
    amount: float | None = Field(default=None, gt=0)
    """Omit for a timer extension with no new bid — which is what a stall is."""
    extension: bool = True


@app.post("/api/lot/{lot_id}/bid")
def bid(lot_id: str, body: BidIn) -> JSONResponse:
    """A bid lands — the half of the recording the demo was ignoring (B-75).

    Suite E's moment classifier reads extension count and post-extension price
    movement. Without this the lot state never changed, so `hot` and `stalled`
    were unreachable in the running app and the nudge could not fire.
    """
    out = get_session().bid(lot_id, body.amount, extension=body.extension)
    if out is None:
        return JSONResponse(
            {"error": "no such live auction lot, or the bid does not raise"},
            status_code=409)
    return JSONResponse(out)


@app.post("/api/reset")
def reset() -> JSONResponse:
    reset_session()
    return JSONResponse({"ok": True})


@app.get("/api/intents")
def intents() -> JSONResponse:
    return JSONResponse({"intents": [i.value for i in Intent]})


# Mounted last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
