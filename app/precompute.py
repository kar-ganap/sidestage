"""D-36b — a warm cache of verified drafts, and why it is safe here.

    warm(lots)        draft + verify the canonical questions for upcoming lots
    lookup(q, lot)    serve one, but only after re-verifying it

WHY A CACHE OF GENERATED TEXT IS NORMALLY A BAD IDEA. The world moves and the
cached answer keeps asserting what was true when it was written. A bid changes,
a lot sells, a comp window ages out — and nothing notices, because text is
unverifiable after the fact.

WHY IT IS SAFE HERE, AND THIS IS D-09 PAYING A DIVIDEND IT WAS NOT BUILT FOR.
Evidence is assembled before generation and every claim cites a fact id, so
**verification costs 1.0 ms p95 of CPU** (measured, `evals/bench.py`). A cached draft can
therefore be re-verified against *freshly assembled* evidence at serve time and
dropped if anything moved. The cached unit is the draft **plus its claims** —
never the text alone, because text alone is exactly what cannot be rechecked.

THE MISTAKE THIS FILE EXISTS TO NOT MAKE (D-36b). The original design keyed the
cache on `(lot, intent)`. That is unsafe in the precise way B-13 describes:
`is that 1st edition?`, `is it shadowless?` and `what set is it from?` are all
`attribute_q` about the same lot with different correct answers, and serving one
to a viewer who asked another produces a reply where **every claim is true and
the answer is about something nobody asked**.

Re-verification does not catch that, and was never going to. It checks claims
against evidence — it guards against the world moving, not against the question
being different. Two threats; the design conflated them.

So the key is `(lot_id, question)`, and a hit needs the incoming question to be a
near-duplicate of the cached one at a **higher** bar than queue clustering uses.
Collapsing two questions wrongly shows one card instead of two — visible and
recoverable. Serving a cached answer wrongly sends the wrong reply to a buyer.

The honest consequence is coverage: this hits on genuine repeats, not on
anything of a known intent. It is a warm cache, not a predictive one.

**MEASURED AND NOT ENABLED (B-31).** The hit rate on the real transcript is
**0/24**, and a *self-warmed* cache — storing exactly what was asked and serving
on any later repeat, which removes the choice of canonical questions from the
question — reaches **1/27 (4%)**. D-36 assumed "per lot the same handful of
questions recur". Twenty-seven seller-directed questions produced twenty-six
distinct ones. Viewers ask about different cards in different words.

This module is therefore **not wired into the live path**. It stays because it is
the evidence for that result, and because `evals/run_precompute.py` reproduces
all of it: the 4% ceiling, re-verification correctly rejecting a hit after a bid
moves, and the count of questions the original unsafe key would have answered
with a reply about something else.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.catalog import Catalog, get_catalog
from app.entities import EntityResolver, get_resolver
from app.evidence import assemble
from app.llm import LLMClient, get_client
from app.models import Draft, Intent, Lot, Verdict
from app.pipeline import draft_reply
from app.triage import _shingle
from app.verify import verify

log = logging.getLogger("sidestage.precompute")

# Higher than the 0.6 used to cluster the queue, and the asymmetry is the reason.
# A wrong cluster costs the operator a glance; a wrong cache hit sends a buyer an
# answer to someone else's question.
HIT_SIMILARITY = 0.85

# The questions worth warming, drawn from what viewers actually asked across 513
# observed messages rather than from what seemed likely. Per lot, not per intent.
CANONICAL: list[tuple[str, Intent]] = [
    ("is that 1st edition?", Intent.ATTRIBUTE_Q),
    ("what set is that from", Intent.ATTRIBUTE_Q),
    ("is it graded", Intent.GRADE_CONDITION_Q),
    ("what grade is it", Intent.GRADE_CONDITION_Q),
    ("hows the centering", Intent.GRADE_CONDITION_Q),
    ("whats the bid at", Intent.PRICE_VALUE_Q),
    ("what have these been selling for", Intent.PRICE_VALUE_Q),
    ("how many left", Intent.AVAILABILITY_Q),
    ("do you ship to canada", Intent.SHIPPING_RETURNS_Q),
]


@dataclass
class Entry:
    """One precomputed draft, with everything needed to recheck it."""

    lot_id: str
    question: str
    intent: Intent
    draft: Draft
    warmed_at: datetime
    shingle: frozenset[str] = field(default_factory=frozenset)
    hits: int = 0
    rejected: int = 0


@dataclass
class Stats:
    warmed: int = 0
    lookups: int = 0
    hits: int = 0
    stale: int = 0          # matched, then failed re-verification
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


class PrecomputeCache:
    def __init__(self, *, catalog: Catalog | None = None,
                 resolver: EntityResolver | None = None,
                 client: LLMClient | None = None,
                 similarity: float = HIT_SIMILARITY) -> None:
        self.catalog = catalog or get_catalog()
        self.resolver = resolver or get_resolver()
        self._client = client
        self.similarity = similarity
        self.entries: dict[str, list[Entry]] = {}      # lot_id -> entries
        self.stats = Stats()

    # -- warm ------------------------------------------------------------

    def warm(self, lots: list[Lot] | None = None, *,
             questions: list[tuple[str, Intent]] | None = None) -> int:
        """Draft and verify ahead of time. Only *passing* drafts are kept.

        Caching a blocked draft would mean serving a block instantly, which is
        worse than useless: the operator gets a refusal that was decided against
        a world that has since moved on, with no chance the live path would have
        answered.
        """
        lots = lots if lots is not None else self._upcoming()
        qs = questions or CANONICAL
        llm = self._client or get_client()
        made = 0
        for lot in lots:
            bucket = self.entries.setdefault(lot.id, [])
            for q, intent in qs:
                r = draft_reply(q, intent=intent, lot=lot, catalog=self.catalog,
                                resolver=self.resolver, client=llm)
                if r.draft.verdict is Verdict.BLOCKED:
                    continue
                bucket.append(Entry(lot_id=lot.id, question=q, intent=intent,
                                    draft=r.draft, warmed_at=datetime.now(UTC),
                                    shingle=_shingle(q)))
                made += 1
        self.stats.warmed += made
        return made

    def _upcoming(self, n: int = 3) -> list[Lot]:
        """The live lot plus the next few queued — D-34's lookahead."""
        live = [l for l in self.catalog.lots.values() if l.status == "live"]
        queued = sorted((l for l in self.catalog.lots.values() if l.status == "queued"),
                        key=lambda l: l.position)
        return (live + queued)[: n + 1]

    # -- serve -----------------------------------------------------------

    def lookup(self, question: str, lot: Lot | None,
               intent: Intent) -> tuple[Draft, int] | None:
        """A cached draft, or None. Re-verified before it is handed back.

        Returns `(draft, microseconds)` so the caller can report what the hit
        actually cost rather than quoting the design's estimate.
        """
        self.stats.lookups += 1
        if lot is None:
            self.stats.misses += 1
            return None
        t0 = time.perf_counter()
        want = _shingle(question)
        if not want:
            self.stats.misses += 1
            return None

        best, best_sim = None, 0.0
        for e in self.entries.get(lot.id, []):
            if e.intent is not intent:
                continue          # necessary, nowhere near sufficient (D-36b)
            sim = (len(want & e.shingle) / len(want | e.shingle)) if e.shingle else 0.0
            if sim > best_sim:
                best, best_sim = e, sim
        if best is None or best_sim < self.similarity:
            self.stats.misses += 1
            return None

        # The whole safety argument, and it costs well under a millisecond:
        # rebuild the evidence
        # from the world as it is NOW and recheck the cached claims against it.
        ev = assemble(intent=intent, resolution=self.resolver.resolve(question),
                      catalog=self.catalog, lot=lot)
        result = verify(best.draft, ev, catalog=self.catalog)
        if result.verdict is not Verdict.PASS:
            best.rejected += 1
            self.stats.stale += 1
            log.info("precompute: dropped a stale hit for %s (%s)",
                     lot.id, [v.code for v in result.violations])
            return None

        best.hits += 1
        self.stats.hits += 1
        return best.draft, int((time.perf_counter() - t0) * 1e6)

    def invalidate(self, lot_id: str) -> int:
        """Drop a lot's entries outright.

        Rarely needed — re-verification already catches a moved world — but a lot
        that ended is worth evicting rather than rechecking on every lookup.
        """
        return len(self.entries.pop(lot_id, []))
