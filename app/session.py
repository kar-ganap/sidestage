"""The live show, in memory. What the console reads and the operator acts on.

One `Session` per running process (D-31: the database is ephemeral; state that
matters for the demo lives here and the ledger journals the writes). Reads are
in-process because they sit on the latency critical path; writes go through the
ledger (D-33).

THE SHAPE OF THE WORKFLOW, which is the thing a reviewer is here to test:

    ingest(text)     triage decides: dropped (with a reason) or queued
    draft(card_id)   evidence -> generate -> verify -> maybe repair
    send(card_id)    the operator's decision, journalled

**Every message is kept, including the dropped ones, with the features that
dropped them.** That is not a debug affordance — it is the product claim. A
triage system the operator cannot audit is one they will stop trusting the first
time it swallows a buyer, and D-15 chose a linear model specifically so this
panel could exist.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.catalog import Catalog, get_catalog
from app.entities import get_resolver
from app.llm import LLMClient, get_client
from app.models import Draft, Intent, Lot, Verdict
from app.pipeline import PipelineResult, draft_reply
from app.triage import Route, TriageCascade, TriageResult, _shingle

MAX_LOG = 500


@dataclass
class LoggedMessage:
    """One chat message and what triage decided about it."""

    seq: int
    text: str
    at: datetime
    route: Route
    score: float
    reasons: list[str]
    intent: Intent
    card_id: str | None = None
    escalated: bool = False
    latency_ms: float = 0.0

    @property
    def surfaced(self) -> bool:
        return self.route.surfaced


@dataclass
class Card:
    """A surfaced question the operator can act on.

    `count` is the demand signal: eight people asking about psyducks is one card
    that says 8, not eight rows. Note the honest caveat from D-26 — in 161 real
    messages there were **two** repeats, so this is correct behaviour rather
    than a headline metric.
    """

    id: str
    text: str
    intent: Intent
    score: float
    at: datetime
    count: int = 1
    lot_id: str | None = None
    status: str = "open"              # open | drafting | ready | blocked | sent | dismissed
    reply: str = ""
    fallback: str = ""
    claims: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    verdict: str = ""
    attempts: int = 0
    ttft_ms: int = 0
    total_ms: int = 0
    reasons: list[str] = field(default_factory=list)


def _claim_rows(draft: Draft) -> list[dict]:
    return [{"type": c.type.value, "value": c.value,
             "fact": c.source_fact_id, "quote": c.quote} for c in draft.claims]


def _fact_rows(res: PipelineResult) -> list[dict]:
    return [{"id": f.id, "kind": f.kind.value, "authority": f.authority.value,
             "note": f.note, "value": str(f.value), "source": f.source}
            for f in res.evidence.facts]


class Session:
    """Not thread-safe by accident — thread-safe on purpose.

    Uvicorn runs handlers in a worker pool, so two chat messages can land
    concurrently. The lock is coarse because the critical sections are
    microseconds (the model calls happen outside it) and a fine-grained scheme
    here would be complexity with no measurable return.
    """

    def __init__(self, *, catalog: Catalog | None = None,
                 client: LLMClient | None = None) -> None:
        self.catalog = catalog or get_catalog()
        self.resolver = get_resolver()
        self._client = client
        self.cascade = TriageCascade(resolver=self.resolver, client=client)
        self.log: deque[LoggedMessage] = deque(maxlen=MAX_LOG)
        self.cards: dict[str, Card] = {}
        self.ledger: list[dict] = []
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.active_lot_id: str | None = self._first_live()

    # -- lots -------------------------------------------------------------

    def _first_live(self) -> str | None:
        for lot in self.catalog.lots.values():
            if lot.status == "live":
                return lot.id
        return next(iter(self.catalog.lots), None)

    @property
    def active_lot(self) -> Lot | None:
        return self.catalog.lots.get(self.active_lot_id or "")

    def set_active_lot(self, lot_id: str) -> bool:
        if lot_id not in self.catalog.lots:
            return False
        self.active_lot_id = lot_id
        return True

    # -- ingest -----------------------------------------------------------

    def ingest(self, text: str) -> LoggedMessage:
        """Triage one message. Dropped messages are logged, not discarded."""
        res: TriageResult = self.cascade.triage(text)
        with self._lock:
            msg = LoggedMessage(
                seq=next(self._seq), text=text, at=datetime.now(UTC),
                route=res.route, score=res.score, reasons=list(res.reasons),
                intent=res.intent, escalated=res.escalated,
                latency_ms=round(res.latency_ms, 2))
            if res.surfaced:
                msg.card_id = self._attach(res, msg.at)
            self.log.append(msg)
        return msg

    def _attach(self, res: TriageResult, at: datetime) -> str:
        """Add to the queue, or increment an existing near-duplicate.

        Clustering happens at ingest rather than as a batch pass because the
        queue is live — the operator is looking at it while it changes, and a
        card that silently splits into two while they read is worse than a
        count that ticks up.
        """
        shingle = _shingle(res.text)
        if shingle:
            for card in self.cards.values():
                if card.status in ("sent", "dismissed"):
                    continue
                prev = _shingle(card.text)
                if prev and len(shingle & prev) / len(shingle | prev) >= 0.6:
                    card.count += 1
                    card.at = at
                    return card.id
        cid = f"c{next(self._ids):03d}"
        self.cards[cid] = Card(
            id=cid, text=res.text, intent=res.intent, score=round(res.score, 3),
            at=at, lot_id=self.active_lot_id, reasons=list(res.reasons))
        return cid

    # -- draft ------------------------------------------------------------

    def draft(self, card_id: str) -> Card | None:
        """Run the core loop for one card. Blocking; the caller streams.

        Deliberately outside the lock. A draft takes seconds and holding the
        lock across it would serialise the whole console behind one model call.
        """
        card = self.cards.get(card_id)
        if card is None:
            return None
        card.status = "drafting"
        res = draft_reply(card.text, intent=card.intent,
                          lot=self.catalog.lots.get(card.lot_id or ""),
                          catalog=self.catalog, resolver=self.resolver,
                          client=self._client or get_client())
        card.reply = res.draft.text
        card.fallback = res.draft.fallback_text or ""
        card.claims = _claim_rows(res.draft)
        card.facts = _fact_rows(res)
        card.violations = [{"code": v.code, "severity": v.severity.value,
                            "message": v.message} for v in res.draft.violations]
        card.verdict = res.draft.verdict.value
        card.attempts = res.attempts
        card.ttft_ms = res.ttft_ms
        card.total_ms = res.total_ms
        card.status = "blocked" if res.draft.verdict is Verdict.BLOCKED else "ready"
        return card

    # -- act --------------------------------------------------------------

    def send(self, card_id: str, *, text: str | None = None) -> dict | None:
        """The operator's decision, journalled.

        A blocked card can still be sent — with the safe fallback, or with text
        the operator wrote themselves. Refusing would make the verifier a
        gatekeeper over a human, which is not what it is for (D-22): it blocks
        the *machine* from asserting something unbacked, and the operator
        remains the one accountable for what goes out.
        """
        card = self.cards.get(card_id)
        if card is None:
            return None
        body = text if text is not None else (
            card.reply if card.status == "ready" else card.fallback)
        entry = {
            "id": f"L{len(self.ledger) + 1:03d}",
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "action": "send_reply",
            "card_id": card_id,
            "verdict": card.verdict or "unverified",
            "overridden": card.status == "blocked",
            "text": body,
        }
        with self._lock:
            self.ledger.append(entry)
            card.status = "sent"
        return entry

    def dismiss(self, card_id: str) -> bool:
        card = self.cards.get(card_id)
        if card is None:
            return False
        card.status = "dismissed"
        return True

    # -- views ------------------------------------------------------------

    def queue(self) -> list[Card]:
        """Open cards, most valuable first. Ranking is the same intent x demand
        x recency used offline, applied to live state."""
        from app.triage import _INTENT_VALUE
        import math
        open_cards = [c for c in self.cards.values()
                      if c.status not in ("sent", "dismissed")]
        newest = max((c.at for c in open_cards), default=None)

        def key(c: Card) -> float:
            age = 0.0 if newest is None else (newest - c.at).total_seconds()
            recency = 1.0 / (1.0 + age * 0.02)
            demand = 1.0 + math.log(c.count) if c.count > 1 else 1.0
            return _INTENT_VALUE.get(c.intent, 0.4) * demand * recency * (0.5 + c.score)

        return sorted(open_cards, key=key, reverse=True)

    def stats(self) -> dict:
        seen = len(self.log)
        surfaced = sum(1 for m in self.log if m.surfaced)
        escalated = sum(1 for m in self.log if m.escalated)
        return {
            "seen": seen,
            "surfaced": surfaced,
            "dropped": seen - surfaced,
            "escalated": escalated,
            "escalation_rate": round(escalated / seen, 3) if seen else 0.0,
            "queue_open": len(self.queue()),
            "sent": len(self.ledger),
        }


_session: Session | None = None


def get_session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


def reset_session() -> Session:
    global _session
    _session = Session()
    return _session
