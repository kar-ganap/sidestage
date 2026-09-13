"""The in-memory catalog: everything a reply can be grounded in.

Loaded once from `data/*.json` at boot and held in dicts. D-33 explains why:
evidence assembly sits on the 1500 ms critical path, and D-09's whole claim is
that verification costs a dictionary lookup rather than a network call. Routing
that through SQL would make the claim false for no benefit at N ~ 300 lots.

Writes are a different story and live in SQLite — see `app/actions/`.

Read `resolve()` in `app/entities.py` for the other half: turning "dragonight"
or "the mew one" into something this store can answer about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.config import DATA_DIR, settings
from app.models import (
    CardSet,
    Comp,
    Grade,
    Item,
    Lot,
    LotFormat,
    Policy,
)

# =====================================================================
# Population reports — third-party and time-varying, so they carry a date
# =====================================================================


@dataclass(frozen=True)
class PopReport:
    """A grader's population figure, with the date it was read.

    Separate from `Comp` because it answers a different question — scarcity
    rather than price — and because it goes stale on its own schedule. Primer §4:
    a pop quoted without an as-of date is a bug, so `as_of` is not optional.
    """

    item_key: str          # "base1:4/102"  (no grade — the grade is a field)
    grader: str
    grade: float
    pop: int
    higher: int
    as_of: datetime

    def age_days(self, now: datetime | None = None) -> int:
        return ((now or datetime.now(UTC)) - self.as_of).days


@dataclass(frozen=True)
class CompWindow:
    """The result of asking "what has this been selling for?"

    Deliberately not a number. Primer §5: a comp claim renders as a range with
    its sample size or it does not ship, so the only thing this type can hand
    back is a range with its sample size — plus `quotable`, which is the
    verifier's answer rather than the caller's opinion.
    """

    item_key: str
    n: int
    low: float
    high: float
    median: float
    window_days: int
    newest: datetime | None
    quotable: bool
    reason: str            # why not, when not — shown to the operator verbatim

    def as_phrase(self) -> str:
        """The only sanctioned way to say this out loud."""
        if not self.quotable:
            return ""
        return f"last {self.n} sold ${self.low:,.0f}–${self.high:,.0f}, past {self.window_days}d"


# =====================================================================
# The store
# =====================================================================


@dataclass
class Catalog:
    """Every read the pipeline makes, in process.

    Built by `Catalog.load()`. Treat as immutable after construction: the
    marketplace adapter owns mutation, and the catalog is re-derived rather than
    patched, so a stale read here is impossible by construction rather than by
    discipline.
    """

    sets: dict[str, CardSet] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    lots: dict[str, Lot] = field(default_factory=dict)
    policies: dict[str, Policy] = field(default_factory=dict)
    comps: dict[str, list[Comp]] = field(default_factory=dict)
    pops: dict[str, PopReport] = field(default_factory=dict)

    nicknames: dict[str, str] = field(default_factory=dict)
    observational_attributes: frozenset[str] = frozenset()
    banned_claims: list[dict[str, Any]] = field(default_factory=list)

    # --- derived indexes, built once in __post_init__ style ------------
    _lots_by_status: dict[str, list[Lot]] = field(default_factory=dict, repr=False)
    _lots_by_item: dict[str, list[Lot]] = field(default_factory=dict, repr=False)
    _policies_by_topic: dict[str, list[Policy]] = field(default_factory=dict, repr=False)

    # -----------------------------------------------------------------
    # Loading
    # -----------------------------------------------------------------

    @classmethod
    def load(cls, data_dir: Path | None = None) -> Catalog:
        d = data_dir or DATA_DIR
        cat = cls()
        cat._load_sets(_read(d / "card_sets.json"))
        cat._load_catalog(_read(d / "catalog.json"))
        cat._load_comps(_read(d / "comps.json"))
        cat._load_policies(_read(d / "policies.json"))
        cat._reindex()
        return cat

    def _load_sets(self, raw: dict[str, Any]) -> None:
        for s in raw["sets"]:
            self.sets[s["code"]] = CardSet(
                code=s["code"],
                name=s["name"],
                era=s["era"],
                released=s["released"],
                # Language-scoped on purpose: English 1st Edition ended in 2003
                # while Japanese printings kept it far longer (primer §3).
                variants_printed={
                    lang: tuple(v) for lang, v in s["variants_printed"].items()
                },
                has_reverse_holo=s.get("has_reverse_holo", True),
            )
        obs = raw.get("observational_attributes", {})
        self.observational_attributes = frozenset(obs.get("attributes", ()))
        self.nicknames = {
            k.lower(): v
            for k, v in raw.get("nicknames", {}).items()
            if not k.startswith("_")
        }

    def _load_catalog(self, raw: dict[str, Any]) -> None:
        for i in raw["items"]:
            g = i.get("grade", {"grader": "RAW"})
            self.items[i["id"]] = Item(
                id=i["id"], game=i["game"], set_code=i["set_code"],
                number=i["number"], name=i["name"], language=i["language"],
                finish=i["finish"], variants=tuple(i.get("variants", ())),
                grade=Grade(
                    grader=g["grader"], value=g.get("value"), cert=g.get("cert"),
                    subgrades=g.get("subgrades"), label=g.get("label"),
                ),
                condition=i.get("condition"), notes=i.get("notes", ""),
                attributes=i.get("attributes", {}),
            )
        for lo in raw["lots"]:
            self.lots[lo["id"]] = Lot(
                id=lo["id"], listing_id=lo["listing_id"], item_id=lo["item_id"],
                title=lo["title"], format=LotFormat(lo["format"]),
                status=lo.get("status", "queued"), position=lo.get("position", 0),
                price=lo.get("price"), quantity=lo.get("quantity", 0),
                floor_price=lo.get("floor_price"), cost_basis=lo.get("cost_basis"),
                starting_bid=lo.get("starting_bid"), current_bid=lo.get("current_bid"),
                reserve=lo.get("reserve"),
                ends_at=_dt(lo.get("ends_at")),
                extensions=lo.get("extensions", 0),
                bid_at_first_extension=lo.get("bid_at_first_extension"),
            )

    def _load_comps(self, raw: dict[str, Any]) -> None:
        for c in raw["comps"]:
            self.comps.setdefault(c["item_key"], []).append(
                Comp(item_key=c["item_key"], price=c["price"],
                     sold_at=_dt(c["sold_at"]), source=c["source"])
            )
        for lst in self.comps.values():
            lst.sort(key=lambda c: c.sold_at, reverse=True)
        for p in raw.get("population", {}).get("reports", []):
            key = f"{p['item_key']}:{p['grader']}{p['grade']:g}"
            self.pops[key] = PopReport(
                item_key=p["item_key"], grader=p["grader"], grade=float(p["grade"]),
                pop=p["pop"], higher=p["higher"], as_of=_dt(p["as_of"]),
            )

    def _load_policies(self, raw: dict[str, Any]) -> None:
        for p in raw["policies"]:
            self.policies[p["id"]] = Policy(
                id=p["id"], topic=p["topic"], text=p["text"],
                min_item_value=p.get("min_item_value"),
            )
        self.banned_claims = list(raw.get("banned_claims", {}).get("rules", []))

    def _reindex(self) -> None:
        for lot in self.lots.values():
            self._lots_by_status.setdefault(lot.status, []).append(lot)
            self._lots_by_item.setdefault(lot.item_id, []).append(lot)
        for lst in self._lots_by_status.values():
            lst.sort(key=lambda x: x.position)
        for pol in self.policies.values():
            self._policies_by_topic.setdefault(pol.topic, []).append(pol)

    # -----------------------------------------------------------------
    # Lots — the show's spine
    # -----------------------------------------------------------------

    @property
    def live_lot(self) -> Lot | None:
        """Whatever is on camera now. The pinned-lot prior anchors here — but
        only weakly, and only when lots are slow (D-16)."""
        live = self._lots_by_status.get("live", [])
        return live[0] if live else None

    def queue(self, limit: int = 5) -> list[Lot]:
        """The next N lots, in order.

        The lookahead's whole basis (D-34): this is knowable *before* the lots
        go live, which is why "is the Umbreon coming up?" answers off a warm
        snapshot instead of a 1500 ms draft.
        """
        return self._lots_by_status.get("queued", [])[:limit]

    def sold(self) -> list[Lot]:
        """Closed lots. Answers "how much did gengar fire red go for?" and
        "did i miss the skyridge" — two observed classes the system can serve
        trivially because it watched every lot close."""
        return self._lots_by_status.get("sold", [])

    def shop(self) -> list[Lot]:
        """Stock that exists but is not in the show queue — the back wall.

        Observed twice, on two platforms: "do you have the dragonight on the back
        wall in your shop??" and "which rayquaza is that in the back?".
        """
        return self._lots_by_status.get("shop", [])

    def lots_for_item(self, item_id: str) -> list[Lot]:
        return self._lots_by_item.get(item_id, [])

    def item_for_lot(self, lot_id: str) -> Item | None:
        lot = self.lots.get(lot_id)
        return self.items.get(lot.item_id) if lot else None

    # -----------------------------------------------------------------
    # Variants — the authority that blocks the demo case
    # -----------------------------------------------------------------

    def variant_was_printed(self, set_code: str, language: str, variant: str) -> bool | None:
        """Did this set ever print this variant in this language?

        Returns None when the set is unknown, which is *not* the same as False —
        an unknown set means we cannot adjudicate, and D-11's default-deny should
        block rather than the verifier asserting a negative it cannot support.

        This is the Champion's Path check. No English card in that set was ever
        printed with a 1st Edition stamp, so a model that confidently says "yes,
        1st edition" — as models do, because "1st edition Charizard" is
        overwhelmingly common in training data — is false by construction rather
        than by judgement.
        """
        cs = self.sets.get(set_code)
        if cs is None:
            return None
        return variant in cs.variants_printed.get(language, ())

    def is_observational(self, attribute: str) -> bool:
        """Attributes that exist only in the card in the seller's hand (D-12).

        Asked five times across 485 observed messages and answered zero times.
        The copilot cannot answer these; it prompts the host to show the card.
        """
        return attribute in self.observational_attributes

    # -----------------------------------------------------------------
    # Comps — never a bare number
    # -----------------------------------------------------------------

    def comp_window(
        self,
        item_key: str,
        *,
        now: datetime | None = None,
        min_n: int | None = None,
        max_age_days: int | None = None,
    ) -> CompWindow:
        """Grade-matched sold comps inside the recency window.

        `item_key` is `set:number:GRADE` — the grade is part of the key rather
        than a filter, because a PSA 9 comp tells you almost nothing about a
        PSA 10 price (primer §5). Mismatching them is the single easiest way to
        be confidently wrong about money.
        """
        now = now or datetime.now(UTC)
        min_n = settings.comp_min_samples if min_n is None else min_n
        max_age = settings.comp_max_age_days if max_age_days is None else max_age_days
        cutoff = now - timedelta(days=max_age)

        rows = self.comps.get(item_key, [])
        fresh = [c for c in rows if c.sold_at >= cutoff]
        prices = sorted(c.price for c in fresh)

        if not rows:
            reason = "no comps on record for this card and grade"
        elif not fresh:
            newest = rows[0].sold_at
            reason = (f"all {len(rows)} comps are older than {max_age}d "
                      f"(newest {(now - newest).days}d ago)")
        elif len(fresh) < min_n:
            reason = f"only {len(fresh)} sales in {max_age}d; need {min_n} to quote a range"
        else:
            reason = ""

        return CompWindow(
            item_key=item_key,
            n=len(fresh),
            low=prices[0] if prices else 0.0,
            high=prices[-1] if prices else 0.0,
            median=prices[len(prices) // 2] if prices else 0.0,
            window_days=max_age,
            newest=fresh[0].sold_at if fresh else (rows[0].sold_at if rows else None),
            quotable=bool(prices) and len(fresh) >= min_n,
            reason=reason,
        )

    def comp_key(self, item: Item) -> str:
        """`set:number:GRADE`. RAW carries its condition, since a raw NM and a
        raw HP are not the same market."""
        g = item.grade
        grade = "RAW-" + (item.condition or "NM") if g.is_raw else f"{g.grader}{g.value:g}"
        return f"{item.set_code}:{item.number}:{grade}"

    def pop_report(self, item: Item) -> PopReport | None:
        g = item.grade
        if g.is_raw or g.value is None:
            return None
        return self.pops.get(f"{item.set_code}:{item.number}:{g.grader}{g.value:g}")

    # -----------------------------------------------------------------
    # Policies
    # -----------------------------------------------------------------

    def policy(self, policy_id: str) -> Policy | None:
        return self.policies.get(policy_id)

    def policies_for(self, topic: str, *, item_value: float | None = None) -> list[Policy]:
        """Clauses on a topic, filtered by the value gate.

        The gate is a field the verifier reads rather than prose the model
        paraphrases — an authenticity clause that applies above $250 is simply
        false below it, however faithfully it is quoted.
        """
        out = self._policies_by_topic.get(topic, [])
        if item_value is None:
            return list(out)
        return [p for p in out
                if p.min_item_value is None or item_value >= p.min_item_value]

    # -----------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------

    def summary(self) -> dict[str, int]:
        return {
            "sets": len(self.sets),
            "items": len(self.items),
            "lots": len(self.lots),
            "live": len(self._lots_by_status.get("live", [])),
            "queued": len(self._lots_by_status.get("queued", [])),
            "sold": len(self._lots_by_status.get("sold", [])),
            "shop": len(self._lots_by_status.get("shop", [])),
            "comp_keys": len(self.comps),
            "comps": sum(len(v) for v in self.comps.values()),
            "pop_reports": len(self.pops),
            "policies": len(self.policies),
            "nicknames": len(self.nicknames),
        }


# =====================================================================
# helpers
# =====================================================================


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _dt(raw: str | None) -> datetime | None:
    """Parse a date or datetime into an aware UTC datetime. None stays None.

    Seed dates are plain `YYYY-MM-DD`; lot timestamps are ISO with a zone.
    Everything downstream compares against `datetime.now(UTC)`, so a naive
    value here would raise at the worst possible moment — inside evidence
    assembly, on the critical path. Hence the tz coercion.

    **It used to return `datetime.now(UTC)` for a missing value**, as a defensive
    default against exactly that crash. That silently fabricated data (B-25):
    only one lot in `catalog.json` carries an `ends_at`, so every other lot —
    including five BIN *shop* listings that have no auction and five that had
    already sold — was handed an auction end time equal to process start.

    Two consequences, and the second is the one that matters. It put a
    microsecond wall-clock into the evidence block, so the draft prompt differed
    between processes and no recorded fixture could ever be replayed. And it
    asserted, to the model, that a fixed-price shop listing was an auction
    closing shortly — a domain falsehood introduced by a default rather than by
    the data.

    `Lot.ends_at` is `datetime | None` precisely so absence is representable.
    Absence is now preserved; the required fields (`sold_at`, `as_of`) index
    rather than `.get()`, so they always receive a string.
    """
    if raw is None:
        return None
    d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    """Process-wide singleton. Rebuilt from JSON at boot (D-31), never patched."""
    global _catalog
    if _catalog is None:
        _catalog = Catalog.load()
    return _catalog


def reload_catalog() -> Catalog:
    """Drop the singleton and rebuild from JSON.

    B-76. The docstring above says "never patched", and that was true until
    `Session.bid()` made lot state mutable so Suite E's moments could be reached
    (B-75). `/api/reset` rebuilt the Session and left the catalog carrying every
    bid the previous run had placed, so a reviewer who reset got a lot at $1,175
    with 18 extensions and a permanently hot nudge.

    Reads being in memory (D-33) is what makes the whole system fast; it also
    means "reset" has to mean reset, and a singleton that anything can mutate
    has to have a way back.
    """
    global _catalog
    _catalog = None
    return get_catalog()
