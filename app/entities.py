"""Turning what people type into something the catalog can answer about.

Buyers do not name cards the way a database does. Across 477 observed messages
they produced seventeen distinct ways to break exact matching:

    dragonight · rakwaza · venasaur · entai · psyducks · quaza · boarders
    Zard · slong zard · big boy gengar · bubble mew · the mew one
    shining dragon · Japanese silver border · gengar fire red
    lugia unseen force · team rocket holos · pokemon delta species

Every one is in `tests/test_entities.py` as a case, because every one came off a
screen rather than out of my head.

Two design commitments, both from D-15 and D-14:

**Explainable, not merely accurate.** Every match carries a `kind` and a `why`,
so "surfaced because it named something you are selling" is answerable in one
sentence. An embedding similarity score cannot do that, which is most of why
there isn't one here.

**Abstention beats a confident guess.** `mew` names a family, not a card — two
Mews sit in the catalog precisely so the correct answer is *"which Mew?"*. A
resolver that picks one is worse than one that asks, because a wrong pick is
indistinguishable from a right one until money has moved.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

from app.catalog import Catalog, get_catalog
from app.models import Item, Lot

# Fuzzy threshold. Deliberately high: at this vocabulary size a loose threshold
# starts matching unrelated Pokemon to each other, and a wrong confident match
# is worse than no match (D-14). Tuned against the observed misspellings — the
# worst real case is `venasaur`→`Venusaur` at 0.88.
FUZZY_MIN = 0.82

# Words that look like qualifiers but are not catalog attributes. Observed:
# "big boy gengar", "bubble mew". Stripping them before matching stops the
# qualifier poisoning the similarity score, and keeping them lets us tell the
# operator what we ignored.
_NOISE_QUALIFIERS = frozenset({
    "big", "boy", "bubble", "the", "a", "an", "that", "this", "one", "ones",
    "your", "you", "got", "have", "any", "some", "pls", "plz", "please",
})

_PUNCT = re.compile(r"[^\w\s]+")
_WS = re.compile(r"\s+")


class MatchKind(StrEnum):
    """How a surface form was resolved. Ordered strongest to weakest."""

    ALIAS = "alias"        # nickname table: "Zard" -> Charizard
    EXACT = "exact"        # the catalog's own spelling
    PLURAL = "plural"      # "psyducks" -> Psyduck
    SET = "set"            # named a set, not a card: "team rocket holos"
    FUZZY = "fuzzy"        # misspelling: "dragonight" -> Dragonite
    DESCRIPTOR = "descriptor"  # description, no name: "Japanese silver border"


@dataclass(frozen=True)
class EntityMatch:
    surface: str          # what they actually typed
    canonical: str        # what it resolved to
    kind: MatchKind
    score: float
    why: str              # operator-facing, one line

    def __str__(self) -> str:
        return f"{self.surface!r} -> {self.canonical} ({self.kind}, {self.score:.2f})"


@dataclass
class Resolution:
    """What a message refers to, and how sure we are.

    `needs_clarification` is the load-bearing field. When it is true the pipeline
    must not draft an answer — it surfaces the question with a clarifying prompt
    instead. See D-14.
    """

    text: str
    matches: list[EntityMatch] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    sets: list[str] = field(default_factory=list)
    ignored_qualifiers: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarification: str = ""
    latency_us: int = 0

    @property
    def resolved(self) -> bool:
        return bool(self.items) and not self.needs_clarification

    @property
    def mentions_catalog(self) -> bool:
        """Naming something currently for sale is high intent regardless of
        phrasing — which is the entire reason this matcher doubles as a triage
        signal (D-15). Half of observed requests carry no question mark."""
        return bool(self.matches)

    def explain(self) -> str:
        if self.needs_clarification:
            return self.clarification
        if not self.matches:
            return "no catalog entity named"
        return " · ".join(m.why for m in self.matches)


# =====================================================================
# Vocabulary — built once from the catalog
# =====================================================================


@dataclass
class _Vocabulary:
    """Every surface form we can recognise, and what it points at."""

    # ONE index, surface form -> every item reachable by it. A single index is
    # what makes ambiguity detectable: "mew" and "mew ex" are different strings
    # but must land in the same bucket, or the two-Mew abstention never fires.
    name_index: dict[str, list[Item]]
    # surface -> a display name, for the `why` line
    display: dict[str, str]
    set_names: dict[str, str]
    aliases: dict[str, str]

    # Card-suffix tokens. A "Mew ex" is still a Mew for the purpose of someone
    # typing "mew", so these are stripped when deriving the head noun.
    SUFFIXES = frozenset({"ex", "v", "vmax", "vstar", "gx", "prime", "star"})

    @classmethod
    def build(cls, cat: Catalog) -> _Vocabulary:
        name_index: dict[str, list[Item]] = {}
        display: dict[str, str] = {}

        def add(surface: str, item: Item, label: str) -> None:
            if not surface:
                return
            bucket = name_index.setdefault(surface, [])
            if item not in bucket:
                bucket.append(item)
            display.setdefault(surface, label)

        for item in cat.items.values():
            full = _norm(item.name)
            add(full, item, item.name)
            toks = [t for t in full.split() if t not in cls.SUFFIXES]
            # Head noun: "mew ex" -> "mew", "shining dragonite" -> "dragonite".
            # Indexed so a bare family name reaches every printing of it, which
            # is the whole mechanism behind "which Mew?".
            if toks:
                add(toks[-1], item, toks[-1].title())
                if len(toks) > 1:
                    add(" ".join(toks), item, item.name)

        set_names: dict[str, str] = {}
        for cs in cat.sets.values():
            set_names[_norm(cs.name)] = cs.code
            set_names[_norm(cs.code)] = cs.code
            # Buyers drop articles and ampersands and singularise set names:
            # "lugia unseen force" for Unseen Forces.
            stripped = _norm(cs.name).replace(" and ", " ").replace("&", "")
            set_names.setdefault(stripped, cs.code)
            if stripped.endswith("s"):
                set_names.setdefault(stripped[:-1], cs.code)
            for w in stripped.split():
                if len(w) > 5:
                    set_names.setdefault(w, cs.code)

        # Aliases point at catalog surface forms, not at prose. "dragonight"
        # must reach whatever the catalog actually calls a Dragonite, which may
        # be "Shining Dragonite" — resolving the target through name_index here
        # means a nickname can never dangle.
        aliases: dict[str, str] = {}
        for nick, target in cat.nicknames.items():
            t = _norm(target)
            if t in name_index:
                aliases[_norm(nick)] = t
                continue
            head = [w for w in t.split() if w not in cls.SUFFIXES]
            if head and head[-1] in name_index:
                aliases[_norm(nick)] = head[-1]

        return cls(name_index, display, set_names, aliases)


# =====================================================================
# Resolution
# =====================================================================


class EntityResolver:
    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog or get_catalog()
        self.vocab = _Vocabulary.build(self.catalog)

    def resolve(self, text: str) -> Resolution:
        t0 = time.perf_counter()
        res = Resolution(text=text)
        norm = _norm(text)
        tokens = norm.split()
        if not tokens:
            res.latency_us = int((time.perf_counter() - t0) * 1e6)
            return res

        # 1. Multi-word forms first, longest first, so "shining dragonite" wins
        #    over "dragonite" and "fire red leaf green" over "red".
        consumed: set[int] = set()
        for width in (4, 3, 2):
            for i in range(len(tokens) - width + 1):
                if any(j in consumed for j in range(i, i + width)):
                    continue
                phrase = " ".join(tokens[i : i + width])
                m = self._match_phrase(phrase)
                if m:
                    res.matches.append(m)
                    consumed.update(range(i, i + width))

        # 2. Single tokens, skipping qualifier noise.
        for i, tok in enumerate(tokens):
            if i in consumed:
                continue
            if tok in _NOISE_QUALIFIERS:
                res.ignored_qualifiers.append(tok)
                continue
            m = self._match_phrase(tok)
            if m:
                res.matches.append(m)
                consumed.add(i)

        self._collect(res)
        res.latency_us = int((time.perf_counter() - t0) * 1e6)
        return res

    # -----------------------------------------------------------------

    def _match_phrase(self, phrase: str) -> EntityMatch | None:
        """Cheapest and most explainable first; fuzzy only as a last resort.

        `canonical` is always a key into `vocab.name_index`, never prose, so
        `_collect` can look it up without a second resolution step.
        """
        v = self.vocab

        if phrase in v.aliases:
            key = v.aliases[phrase]
            return EntityMatch(phrase, key, MatchKind.ALIAS, 1.0,
                               f"{phrase!r} is a known nickname for {v.display[key]}")

        if phrase in v.name_index:
            return EntityMatch(phrase, phrase, MatchKind.EXACT, 1.0,
                               f"names {v.display[phrase]}")

        # "psyducks" -> psyduck. Before fuzzy, so a plural never drifts to a
        # different card on a similarity score.
        if phrase.endswith("s") and phrase[:-1] in v.name_index:
            key = phrase[:-1]
            return EntityMatch(phrase, key, MatchKind.PLURAL, 0.98,
                               f"plural of {v.display[key]}")

        if phrase in v.set_names:
            code = v.set_names[phrase]
            return EntityMatch(phrase, code, MatchKind.SET, 1.0,
                               f"names the set {self.catalog.sets[code].name}")

        # Fuzzy last, against card surfaces only. Set names are long enough that
        # fuzzy-matching them yields more noise than signal. Short strings are
        # excluded because at 3-4 characters almost everything is 0.8 similar to
        # almost everything else.
        n = len(phrase)
        if n < 5:
            return None

        # Length pruning, and it is a PROOF rather than a heuristic — which
        # matters, because a heuristic here could silently drop a real match.
        #
        #   ratio = 2M / (n + m),  and M <= min(n, m)
        #
        # so for ratio >= FUZZY_MIN with m > n:  2n/(n+m) >= F  =>  m <= n(2/F - 1)
        # and symmetrically                                        m >= n/(2/F - 1)
        #
        # Any surface outside that band cannot reach the threshold no matter how
        # its characters line up, so skipping it changes no outcome. At F=0.82
        # the band is roughly [0.69n, 1.44n], which prunes ~75% of a large
        # vocabulary and keeps this O(catalog) loop affordable as N grows.
        span = 2.0 / FUZZY_MIN - 1.0
        lo, hi = n / span, n * span

        best, best_score = None, 0.0
        for surface in v.name_index:
            if not (lo <= len(surface) <= hi):
                continue
            s = SequenceMatcher(None, phrase, surface).ratio()
            if s > best_score:
                best, best_score = surface, s
        if best and best_score >= FUZZY_MIN:
            return EntityMatch(phrase, best, MatchKind.FUZZY, best_score,
                               f"{phrase!r} looks like {v.display[best]} ({best_score:.0%})")
        return None

    def _collect(self, res: Resolution) -> None:
        """Turn matches into candidate items and lots, and decide whether to ask."""
        cat = self.catalog

        named_sets = {m.canonical for m in res.matches if m.kind is MatchKind.SET}
        res.sets = sorted(named_sets)

        items: list[Item] = []
        for m in res.matches:
            if m.kind is MatchKind.SET:
                continue
            items.extend(self.vocab.name_index.get(m.canonical, []))

        # A named set narrows a named card: "gengar fire red" is one card even
        # though "gengar" alone might be several. Two weak signals combining
        # into one strong one is the whole reason set names are indexed at all.
        if named_sets and items:
            narrowed = [i for i in items if i.set_code in named_sets]
            if narrowed:
                items = narrowed

        # A set with no card names it: "team rocket holos", "pokemon delta
        # species". Offer the set's lots rather than nothing.
        if named_sets and not items:
            items = [i for i in cat.items.values() if i.set_code in named_sets]

        seen: set[str] = set()
        res.items = [i for i in items if not (i.id in seen or seen.add(i.id))]
        res.lots = [lot for i in res.items for lot in cat.lots_for_item(i.id)]

        if len(res.items) > 1:
            res.needs_clarification = True
            res.clarification = self._ask(res)

    def _ask(self, res: Resolution) -> str:
        """The question to put back to the room.

        Phrased with the distinguishing attribute rather than internal ids,
        because the operator reads this aloud.
        """
        names = {i.name for i in res.items}
        if len(names) == 1:
            # Same name, different printings — the "which Mew?" case is usually
            # this: distinguish by set and grade, which is what a buyer cares about.
            opts = [f"{self.catalog.sets[i.set_code].name}"
                    f"{'' if i.grade.is_raw else f' {i.grade.grader} {i.grade.value:g}'}"
                    for i in res.items]
            return f"which {next(iter(names))}? — {' or '.join(opts)}"
        return "which one? — " + " or ".join(sorted(names))


# =====================================================================
# helpers
# =====================================================================


def _norm(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Accent stripping matters less for English card names than for the fact that
    users paste text from anywhere; punctuation stripping matters because
    `Blazikens ?` and `Blazikens?` and `Blazikens` must all be one token.
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()


_resolver: EntityResolver | None = None


def get_resolver() -> EntityResolver:
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver()
    return _resolver
