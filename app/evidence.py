"""Assembling the fact bundle, before a word is generated.

This is the load-bearing half of D-09. Everything the model is allowed to
assert is fetched *first*, given a short id, and handed over as its entire
universe. Two things follow, and both matter:

**Verification becomes a dictionary lookup.** `verify()` never calls out to
anything — the fetch was already paid for. That is the only reason safety fits
inside a 1500 ms budget while still being real.

**Refusal becomes citable.** An attribute the copilot must not assert — the back
of a card, raw centring, holo swirl — is not simply left out. It gets a fact of
its own saying *"observational: defer to the host"*. The model can then point at
that fact while declining, instead of producing an unsourced apology that D-11's
default-deny would have to block. Abstention with a citation beats abstention by
omission: one is an answer, the other is a gap the model will try to fill.

Facts carry a TTL because an evidence snapshot goes stale *inside* the request.
Roughly 800 ms elapses between assembly and verification, and on an auction lot
a bid lands in that window — see `refresh()`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.catalog import Catalog, CompWindow, get_catalog
from app.config import settings
from app.entities import Resolution
from app.models import (
    Authority,
    ClaimType,
    Evidence,
    Fact,
    Intent,
    Item,
    Lot,
    LotFormat,
)

# Live values re-read at verify time. Short, because the window they guard
# against is the ~800 ms between assembly and verification.
_LIVE_TTL_S = 2

# Which fact families each intent needs. Assembling everything would cost
# tokens and, worse, give the model material to wander into: a question about
# shipping does not benefit from having comps in front of it.
_NEEDS: dict[Intent, frozenset[ClaimType]] = {
    Intent.ATTRIBUTE_Q: frozenset({ClaimType.IDENTITY, ClaimType.VARIANT, ClaimType.CONDITION}),
    Intent.GRADE_CONDITION_Q: frozenset({ClaimType.IDENTITY, ClaimType.GRADE,
                                         ClaimType.CENTERING, ClaimType.CONDITION}),
    Intent.PRICE_VALUE_Q: frozenset({ClaimType.IDENTITY, ClaimType.PRICE, ClaimType.BID,
                                     ClaimType.COMP, ClaimType.POP}),
    Intent.AVAILABILITY_Q: frozenset({ClaimType.IDENTITY, ClaimType.AVAILABILITY,
                                      ClaimType.PRICE, ClaimType.BID}),
    Intent.SHIPPING_RETURNS_Q: frozenset({ClaimType.SHIPPING, ClaimType.RETURNS}),
    Intent.AUTHENTICITY_Q: frozenset({ClaimType.IDENTITY, ClaimType.AUTHENTICITY,
                                      ClaimType.PRICE, ClaimType.GRADE}),
    Intent.REQUEST: frozenset({ClaimType.IDENTITY, ClaimType.AVAILABILITY, ClaimType.PRICE}),
    Intent.MARKET_COMMENT: frozenset({ClaimType.IDENTITY, ClaimType.COMP, ClaimType.POP}),
    Intent.NEGOTIATION: frozenset({ClaimType.IDENTITY, ClaimType.PRICE, ClaimType.BID,
                                   ClaimType.COMP}),
}
# Anything unlisted gets the wide set — `unknown` routes here, and D-11's
# default-deny is what keeps a wide bundle safe rather than a prompt instruction.
_WIDE = frozenset(ClaimType)


class _Mint:
    """Sequential short ids. The model pays per token to cite these, so `f7`
    rather than a uuid is a real saving at the rate the drafting path runs."""

    def __init__(self) -> None:
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return f"f{self._n}"


# =====================================================================
# Assembly
# =====================================================================


def assemble(
    *,
    intent: Intent,
    resolution: Resolution,
    catalog: Catalog | None = None,
    lot: Lot | None = None,
    now: datetime | None = None,
) -> Evidence:
    """Build the model's entire universe for one question.

    `lot` overrides the resolver — used when the operator has clicked a specific
    card, and when the pinned-lot prior applies. Otherwise the resolved entity
    decides, and if nothing resolved we still assemble policy and queue facts so
    a catalog-level question can be answered.
    """
    cat = catalog or get_catalog()
    now = now or datetime.now(UTC)
    t0 = datetime.now(UTC)
    mint = _Mint()
    want = _NEEDS.get(intent, _WIDE)
    facts: list[Fact] = []

    # Ambiguity is evidence, and it goes first so it is impossible to miss.
    # Without this the model receives two Mews and no instruction, and it will
    # pick one — which is the failure D-14 exists to prevent. With it, "which
    # Mew?" is a claim the model can cite rather than a hedge it improvises.
    if resolution.needs_clarification and lot is None:
        facts.append(Fact(
            id=mint(), kind=ClaimType.IDENTITY, subject="resolution",
            # B-48. `candidates` used to be bare names — ["Mew", "Mew ex"] —
            # which is not enough to ask a distinguishing question with. The
            # model has to name what separates the candidates (set, number,
            # grade), so those have to be IN the fact: anything it says that is
            # not in the fact it cites is an unbacked claim, and the clarifier
            # was blocking on its own correct disambiguators. Evidence must
            # carry what the reply is asked to distinguish on.
            value={"ambiguous": True, "question": resolution.clarification,
                   "candidates": [_candidate(i, cat) for i in resolution.items]},
            authority=Authority.RECORD, source="entities.resolve", as_of=now,
            note=f"AMBIGUOUS — ask, do not answer: {resolution.clarification}",
        ))

    subject = lot or _subject_lot(resolution, cat)
    item = cat.items.get(subject.item_id) if subject else None

    if subject and item:
        facts += _identity(mint, subject, item, cat, want)
        facts += _variant(mint, item, cat, now, want)
        facts += _grade(mint, item, now, want)
        facts += _pricing(mint, subject, now, want)
        facts += _market(mint, item, cat, now, want)
        facts += _observational(mint, item, cat, now, want)

    facts += _policy(mint, subject, cat, now, want)
    facts += _queue(mint, resolution, cat, now, want, subject)

    return Evidence(
        id=f"ev_{uuid.uuid4().hex[:10]}",
        lot_id=subject.id if subject else None,
        facts=tuple(facts),
        built_at=now,
        assembly_ms=int((datetime.now(UTC) - t0).total_seconds() * 1000),
    )


def _candidate(item: Item, cat: Catalog) -> dict:
    """Enough to tell this candidate apart from the others (B-48).

    Set NAME rather than code, because that is what a buyer in chat would
    recognise and therefore what the clarifier has to be able to say.
    """
    s = cat.sets.get(item.set_code)
    g = item.grade
    return {
        "item_id": item.id,
        "name": item.name,
        "set": s.name if s else item.set_code,
        "number": item.number,
        "grade": (f"{g.grader} {g.value:g}" if g and g.value is not None
                  else (g.grader if g else "RAW")),
    }


def _subject_lot(res: Resolution, cat: Catalog) -> Lot | None:
    """Which lot is this about?

    D-16: the pinned-lot prior is the fallback, not the default. When a message
    names something, what it named wins — observation put only ~22% of
    seller-directed traffic on the item currently on camera.
    """
    if res.needs_clarification:
        return None            # nothing to ground in until the room answers
    if res.lots:
        live = [x for x in res.lots if x.status == "live"]
        return live[0] if live else res.lots[0]
    return cat.live_lot


# =====================================================================
# Fact families
# =====================================================================


def _identity(m, lot: Lot, item: Item, cat: Catalog, want) -> list[Fact]:
    if ClaimType.IDENTITY not in want:
        return []
    cs = cat.sets.get(item.set_code)
    return [Fact(
        id=m(), kind=ClaimType.IDENTITY, subject=lot.id,
        value={"name": item.name, "set": cs.name if cs else item.set_code,
               "number": item.number, "language": item.language, "finish": item.finish},
        authority=Authority.RECORD, source=f"catalog.items.{item.id}",
        as_of=cat_now(), note=f"{item.name} — {cs.name if cs else item.set_code} {item.number}",
    )]


def _variant(m, item: Item, cat: Catalog, now, want) -> list[Fact]:
    """What this copy has, and separately what the set ever printed.

    Two facts, deliberately. A set-catalog check alone passes a card that COULD
    have been 1st Edition but isn't; a record check alone cannot say that a
    claimed variant is impossible. The verifier needs both to tell "this copy
    doesn't have it" apart from "no copy could".
    """
    if ClaimType.VARIANT not in want:
        return []
    out = [Fact(
        id=m(), kind=ClaimType.VARIANT, subject=item.id,
        value={"variants": list(item.variants)},
        authority=Authority.RECORD, source=f"catalog.items.{item.id}.variants",
        as_of=now,
        note=("no special variant on this copy" if not item.variants
              else "this copy is " + ", ".join(v.replace("_", " ") for v in item.variants)),
    )]
    cs = cat.sets.get(item.set_code)
    if cs:
        printed = cs.variants_printed.get(item.language, ())
        out.append(Fact(
            id=m(), kind=ClaimType.VARIANT, subject=item.set_code,
            value={"printed": list(printed), "language": item.language,
                   "has_reverse_holo": cs.has_reverse_holo},
            authority=Authority.CATALOG,
            source=f"card_sets.{cs.code}.variants_printed.{item.language}",
            as_of=now,
            note=(f"{cs.name} ({item.language}) was printed as: "
                  + (", ".join(printed) if printed else "no variants recorded")),
        ))
    return out


def _grade(m, item: Item, now, want) -> list[Fact]:
    if ClaimType.GRADE not in want:
        return []
    g = item.grade
    out = [Fact(
        id=m(), kind=ClaimType.GRADE, subject=item.id,
        value={"grader": g.grader, "value": g.value, "cert": g.cert, "label": g.label,
               "raw": g.is_raw, "condition": item.condition},
        authority=Authority.RECORD, source=f"catalog.items.{item.id}.grade", as_of=now,
        note=(f"raw, condition {item.condition}" if g.is_raw
              else f"{g.grader} {g.value:g}, cert {g.cert}"),
    )]
    # Centring is only assertable when a grader wrote it down as a subgrade.
    # On a PSA slab it is merely implied by the grade, and on a raw card it is
    # observational — see `_observational`.
    if g.subgrades and ClaimType.CENTERING in want:
        out.append(Fact(
            id=m(), kind=ClaimType.CENTERING, subject=item.id,
            value=g.subgrades, authority=Authority.RECORD,
            source=f"catalog.items.{item.id}.grade.subgrades", as_of=now,
            note="BGS subgrades: " + ", ".join(f"{k} {v:g}" for k, v in g.subgrades.items()),
        ))
    return out


def _pricing(m, lot: Lot, now, want) -> list[Fact]:
    """Price, bid state and stock. All TTL'd — these are the values that move.

    On an auction lot this is the concrete case D-09 exists for: a bid can land
    between assembly and verification, and a reply quoting the old number is
    wrong in a way nobody would notice until it mattered.
    """
    out: list[Fact] = []
    if lot.format is LotFormat.BIN:
        if ClaimType.PRICE in want and lot.price is not None:
            out.append(Fact(
                id=m(), kind=ClaimType.PRICE, subject=lot.id,
                value={"price": lot.price, "currency": "USD"},
                authority=Authority.RECORD, source=f"lots.{lot.id}.price",
                as_of=now, ttl_s=_LIVE_TTL_S, note=f"${lot.price:,.2f}",
            ))
        if ClaimType.AVAILABILITY in want:
            out.append(Fact(
                id=m(), kind=ClaimType.AVAILABILITY, subject=lot.id,
                value={"quantity": lot.quantity, "status": lot.status},
                authority=Authority.RECORD, source=f"lots.{lot.id}.quantity",
                as_of=now, ttl_s=_LIVE_TTL_S,
                note=f"{lot.quantity} available" if lot.quantity else "sold out",
            ))
    else:
        if ClaimType.BID in want:
            out.append(Fact(
                id=m(), kind=ClaimType.BID, subject=lot.id,
                value={"current_bid": lot.current_bid, "starting_bid": lot.starting_bid,
                       "reserve_met": (lot.reserve is None or
                                       (lot.current_bid or 0) >= lot.reserve),
                       "ends_at": lot.ends_at.isoformat() if lot.ends_at else None},
                authority=Authority.RECORD, source=f"lots.{lot.id}.current_bid",
                as_of=now, ttl_s=_LIVE_TTL_S,
                note=(f"auction, current bid ${lot.current_bid:,.2f}"
                      if lot.current_bid else "auction, no bids yet"),
            ))
        # The reserve is real and checkable, but it is the SELLER'S number and
        # saying it out loud destroys their position. Marked so the verifier can
        # let the operator see it while blocking any claim that quotes it.
        if lot.reserve is not None and ClaimType.PRICE in want:
            out.append(Fact(
                id=m(), kind=ClaimType.PRICE, subject=lot.id,
                value={"reserve": lot.reserve, "operator_only": True},
                authority=Authority.RECORD, source=f"lots.{lot.id}.reserve",
                as_of=now, ttl_s=_LIVE_TTL_S,
                note=f"reserve ${lot.reserve:,.2f} — OPERATOR ONLY, never quote to the room",
            ))
    if ClaimType.AVAILABILITY in want:
        out.append(Fact(
            id=m(), kind=ClaimType.AVAILABILITY, subject=lot.id,
            value={"status": lot.status, "position": lot.position},
            authority=Authority.RECORD, source=f"lots.{lot.id}.status", as_of=now,
            note=f"lot is {lot.status}",
        ))
    return out


def _market(m, item: Item, cat: Catalog, now, want) -> list[Fact]:
    """Comps and population. Both third-party, both time-varying.

    The comp fact carries the whole `CompWindow` including `quotable` and the
    reason it isn't — so a blocked comp can be explained to the operator in the
    catalog's own words rather than the verifier's.
    """
    out: list[Fact] = []
    if ClaimType.COMP in want:
        w: CompWindow = cat.comp_window(cat.comp_key(item), now=now)
        out.append(Fact(
            id=m(), kind=ClaimType.COMP, subject=item.id,
            value={"n": w.n, "low": w.low, "high": w.high, "median": w.median,
                   "window_days": w.window_days, "quotable": w.quotable,
                   "reason": w.reason, "phrase": w.as_phrase()},
            authority=Authority.THIRD_PARTY, source=f"comps.{w.item_key}",
            as_of=w.newest or now,
            ttl_s=None,
            note=w.as_phrase() if w.quotable else f"NOT QUOTABLE — {w.reason}",
        ))
    if ClaimType.POP in want:
        p = cat.pop_report(item)
        if p:
            stale = p.age_days(now) > settings.comp_max_age_days
            out.append(Fact(
                id=m(), kind=ClaimType.POP, subject=item.id,
                value={"pop": p.pop, "higher": p.higher, "grader": p.grader,
                       "grade": p.grade, "as_of": p.as_of.date().isoformat(),
                       "age_days": p.age_days(now), "stale": stale},
                authority=Authority.THIRD_PARTY,
                source=f"population.{p.item_key}.{p.grader}{p.grade:g}", as_of=p.as_of,
                note=(f"pop {p.pop:,}, {p.higher:,} higher (read "
                      f"{p.as_of.date().isoformat()})"
                      + (" — STALE, do not quote" if stale else "")),
            ))
    return out


def _observational(m, item: Item, cat: Catalog, now, want) -> list[Fact]:
    """A fact for each thing we are NOT allowed to assert.

    The point of making refusal citable: the model declines while pointing at a
    fact, instead of improvising a hedge that D-11 would have to block. It also
    gives the console something to show — "5 people want to see the back" is a
    presentation prompt the seller can act on, which is the only real answer to
    the most-asked question in the observed corpus.
    """
    if not (want & {ClaimType.CONDITION, ClaimType.CENTERING, ClaimType.VARIANT}):
        return []
    relevant = sorted(cat.observational_attributes)
    if item.grade.is_raw:
        pass                      # everything below applies
    else:
        # In a slab the back and edges are sealed and visible; centring is still
        # only assertable from a subgrade.
        relevant = [a for a in relevant if a not in {"back_condition", "edge_wear"}]
    if not relevant:
        return []
    return [Fact(
        id=m(), kind=ClaimType.CONDITION, subject=item.id,
        value={"observational": relevant, "assertable": False},
        authority=Authority.OBSERVATIONAL, source="card_sets.observational_attributes",
        as_of=now,
        note=("these exist only in the card in the seller's hand and must never be "
              "asserted — defer to the host: " + ", ".join(a.replace("_", " ") for a in relevant)),
    )]


def _policy(m, lot: Lot | None, cat: Catalog, now, want) -> list[Fact]:
    """Policy clauses, value-gated.

    The gate is a field the verifier reads rather than prose the model
    paraphrases: an authenticity clause that applies above $250 is simply false
    below it, however faithfully it is quoted.
    """
    topics = []
    if ClaimType.SHIPPING in want:
        topics += ["shipping", "combined_shipping"]
    if ClaimType.RETURNS in want:
        topics += ["returns"]
    if ClaimType.AUTHENTICITY in want:
        topics += ["authenticity", "grading"]
    if not topics:
        return []

    value = None
    if lot is not None:
        value = lot.price if lot.format is LotFormat.BIN else lot.current_bid

    out: list[Fact] = []
    for topic in dict.fromkeys(topics):
        for pol in cat.policies_for(topic, item_value=value):
            out.append(Fact(
                id=m(), kind=_POLICY_KIND.get(topic, ClaimType.RETURNS), subject=pol.id,
                value={"text": pol.text, "clause": pol.id,
                       "min_item_value": pol.min_item_value},
                authority=Authority.RECORD, source=f"policies.{pol.id}", as_of=now,
                note=pol.text,
            ))
    return out


_POLICY_KIND = {
    "shipping": ClaimType.SHIPPING, "combined_shipping": ClaimType.SHIPPING,
    "returns": ClaimType.RETURNS, "authenticity": ClaimType.AUTHENTICITY,
    "grading": ClaimType.GRADE,
}


def _queue(m, res: Resolution, cat: Catalog, now, want,
           subject: Lot | None = None) -> list[Fact]:
    """Where things sit in the show.

    D-34: the queue is knowable before lots go live, which is why "is the
    Umbreon coming up?" answers off a warm snapshot. Observed traffic put the
    majority of seller-directed messages on the queue, the shop or a closed lot
    rather than on the card in hand.
    """
    if ClaimType.AVAILABILITY not in want:
        return []
    out: list[Fact] = []
    named = {i.id for i in res.items}

    # B-71. These used to carry `starting_bid` and `price` INSIDE the
    # availability fact, so a money figure lived in two facts of two kinds at
    # once — and the model, asked "how many blastoise do you have left", cited
    # the availability fact for its price claim and was blocked for
    # mis-citation. It had picked one of the two places the number was.
    #
    # The invariant now: **each assertable value lives in exactly one fact, of
    # the kind that can assert it.** Duplication across kinds is how B-04
    # mis-citation happens, and the model is not at fault for choosing wrong
    # between two right answers.
    here = subject.id if subject else None

    for lot in cat.queue(limit=8):
        if (named and lot.item_id not in named) or lot.id == here:
            continue        # the subject lot is already covered by _pricing
        out.append(Fact(
            id=m(), kind=ClaimType.AVAILABILITY, subject=lot.id,
            value={"status": "queued", "position": lot.position, "title": lot.title},
            authority=Authority.RECORD, source=f"lots.{lot.id}", as_of=now,
            note=f"coming up at position {lot.position}: {lot.title}",
        ))
        # An opening bid is BID state, not a price — the same kind `_pricing`
        # gives the subject lot, so the model does not have to guess which kind
        # a queued lot's figure is depending on whether it is the one on camera.
        # Only when the buyer named something. "Is the umbreon coming up?" wants
        # a position, not the opening bid of eight other lots — and every fact
        # minted is prompt the model pays for on a latency-bound path.
        if named and lot.starting_bid is not None and ClaimType.BID in want:
            out.append(Fact(
                id=m(), kind=ClaimType.BID, subject=lot.id,
                value={"current_bid": None, "starting_bid": lot.starting_bid,
                       "reserve_met": False, "ends_at": None},
                authority=Authority.RECORD,
                source=f"lots.{lot.id}.starting_bid", as_of=now,
                note=f"{lot.title}: not open yet, opening bid "
                     f"${lot.starting_bid:,.2f}",
            ))
    for lot in cat.shop():
        if (named and lot.item_id not in named) or lot.id == here:
            continue
        out.append(Fact(
            id=m(), kind=ClaimType.AVAILABILITY, subject=lot.id,
            value={"status": "shop", "quantity": lot.quantity, "title": lot.title},
            authority=Authority.RECORD, source=f"lots.{lot.id}", as_of=now,
            note=f"in the shop: {lot.title}, {lot.quantity} available",
        ))
        if named and lot.price is not None and ClaimType.PRICE in want:
            out.append(Fact(
                id=m(), kind=ClaimType.PRICE, subject=lot.id,
                value={"price": lot.price, "currency": "USD"},
                authority=Authority.RECORD, source=f"lots.{lot.id}.price",
                as_of=now, ttl_s=_LIVE_TTL_S,
                note=f"{lot.title}: ${lot.price:,.2f}",
            ))
    for lot in cat.sold():
        if named and lot.item_id not in named:
            continue
        closed = lot.current_bid if lot.format is LotFormat.AUCTION else lot.price
        out.append(Fact(
            id=m(), kind=ClaimType.PRICE, subject=lot.id,
            value={"status": "sold", "closed_at": closed, "title": lot.title},
            authority=Authority.RECORD, source=f"lots.{lot.id}", as_of=now,
            note=f"already sold: {lot.title} closed at ${closed:,.0f}" if closed else "sold",
        ))
    return out


# =====================================================================
# Staleness — the snapshot decays inside the request
# =====================================================================


@dataclass(frozen=True)
class StaleFact:
    fact: Fact
    was: object
    now: object

    def __str__(self) -> str:
        return f"{self.fact.kind} on {self.fact.subject} moved {self.was!r} -> {self.now!r}"


def refresh(ev: Evidence, catalog: Catalog | None = None,
            now: datetime | None = None) -> list[StaleFact]:
    """Re-read every TTL'd fact and report what moved.

    Called at verify time, not draft time. About 800 ms passes between the two,
    and on a contested auction lot bids were observed landing every few seconds
    — so a reply quoting the bid from assembly time can be wrong by the moment
    it is sent. A moved fact is its own violation; it does not silently update,
    because the draft was written against the old value and its claims may no
    longer follow.
    """
    cat = catalog or get_catalog()
    now = now or datetime.now(UTC)
    moved: list[StaleFact] = []
    for f in ev.facts:
        if f.ttl_s is None or (now - f.as_of).total_seconds() <= f.ttl_s:
            continue
        lot = cat.lots.get(f.subject)
        if lot is None:
            continue
        current = _live_value(f.kind, lot)
        if current is not None and current != _recorded_value(f):
            moved.append(StaleFact(fact=f, was=_recorded_value(f), now=current))
    return moved


def _live_value(kind: ClaimType, lot: Lot):
    if kind is ClaimType.BID:
        return lot.current_bid
    if kind is ClaimType.PRICE:
        return lot.price
    if kind is ClaimType.AVAILABILITY:
        return lot.quantity
    return None


def _recorded_value(f: Fact):
    v = f.value
    if not isinstance(v, dict):
        return v
    for k in ("current_bid", "price", "quantity"):
        if k in v:
            return v[k]
    return None


def cat_now() -> datetime:
    return datetime.now(UTC)
