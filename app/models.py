"""The vocabulary of the system.

Everything else in SideStage is written in terms of these types, so this file is
worth reading first. The load-bearing idea is the pair at the centre:

    a CLAIM cites a FACT.

A model may only assert things it can point at. `Evidence` is the bundle of facts
assembled *before* generation; `Claim` is the model's assertion plus the id of the
fact backing it; `verify()` is then a set of small local lookups rather than a
judgement call. See docs/DECISIONS.md D-09, D-10, D-11.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# =====================================================================
# Enumerations
# =====================================================================


class Intent(StrEnum):
    """Ten classes plus `UNKNOWN` (D-13).

    The class determines two things: which evidence to assemble, and how high up
    the automation ladder this kind of question is allowed to climb (D-22).
    `UNKNOWN` is a route, not a failure — the question still surfaces, just
    without an automatic draft.
    """

    ATTRIBUTE_Q = "attribute_q"            # "1st ed?" "shadowless?" "reverse holo?"
    GRADE_CONDITION_Q = "grade_condition_q"  # "centering?" "whitening?" "cert #?"
    PRICE_VALUE_Q = "price_value_q"        # "what's it going for?" "last sold?"
    AVAILABILITY_Q = "availability_q"      # "how many left?" "any blazikens?"
    SHIPPING_RETURNS_Q = "shipping_returns_q"  # highest volume, lowest risk
    AUTHENTICITY_Q = "authenticity_q"      # "is it authenticated?" — human-gated
    BUY_COMMIT = "buy_commit"              # "I'll take it" -> proposes an ACTION
    NEGOTIATION = "negotiation"            # "$350 shipped?" — human-gated
    HYPE_NOISE = "hype_noise"              # dropped, but counted for the velocity signal
    OFF_TOPIC_ABUSE = "off_topic_abuse"

    # --- added 2026-09-12 after the first field observation (D-13 amendment) ---

    REQUEST = "request"
    """"run the shining dragon" · "lugia next!" · "Back again plz" · "Go quicker".

    The highest-intent traffic in the room and the class the armchair taxonomy
    missed entirely. Buyers do not ask whether you have something — they ask you
    to sell it to them now. Maps onto `swap_showcase` / `push_lot` rather than
    onto a reply, and roughly half of these carry no question mark, which is
    exactly where the platform's own highlighting fails.
    """

    MARKET_COMMENT = "market_comment"
    """"8 is 3 k" · "1.5 in a 7, 1.3 in a 6" · "Check comps" · "Psa10 77k".

    Viewers supplying the comps and population figures the seller never states.
    At 13.7% of observed traffic this is the largest non-social class — it
    outnumbers every category of question combined, which makes it the clearest
    demand signal in the data. Never auto-replied to; it feeds the nudge layer.
    """

    CROSS_USER = "cross_user"
    """Viewers addressing each other, not the seller.

    24% of observed traffic, and it carries question marks while not being
    questions to us — "base set?" was one viewer asking another about a card
    that viewer owns. Surfacing one is a false positive, and a conspicuous one.
    """

    SYSTEM_EVENT = "system_event"
    """Platform-generated: "Unlocked Bronze", "is raiding with a party of 32".

    Not a user message at all, so it never enters the scorer. Some are genuine
    signal — a raid moved 32 viewers in at once, ~16% of the room — and feed the
    engagement layer rather than the reply queue.
    """

    UNKNOWN = "unknown"


class Authority(StrEnum):
    """Where a fact gets its force (D-12).

    The distinction that matters is OBSERVATIONAL: centering on a raw card, holo
    swirl, print lines, whitening. Those exist only in the physical card in the
    seller's hand. They are unfalsifiable from here, therefore unverifiable,
    therefore the copilot never asserts them — it defers to the host.
    """

    RECORD = "record"                # the listing itself
    CATALOG = "catalog"              # set-level print facts; what was ever printed
    THIRD_PARTY = "third_party"      # pop reports, comps — time-varying, need an as-of
    OBSERVATIONAL = "observational"  # only the seller can see it


class ClaimType(StrEnum):
    """Shared vocabulary for `Fact.kind` and `Claim.type`.

    Deliberately one enum rather than two. Because a claim's type names the same
    thing as the kind of fact that can support it, verification reduces to a
    dictionary lookup into a registry of per-type verifiers.
    """

    IDENTITY = "identity"          # set / number / name
    VARIANT = "variant"            # 1st edition, shadowless, reverse holo
    GRADE = "grade"
    CENTERING = "centering"
    CONDITION = "condition"
    POP = "pop"
    COMP = "comp"                  # sold comps — never a bare number (primer §5)
    PRICE = "price"
    BID = "bid"
    AVAILABILITY = "availability"
    SHIPPING = "shipping"
    RETURNS = "returns"
    AUTHENTICITY = "authenticity"
    SIZING = "sizing"              # fashion slice — mostly OBSERVATIONAL (D-02)


class Severity(StrEnum):
    """Whether a rewrite could possibly help.

    This is a latency decision as much as a correctness one. An UNREPAIRABLE
    violation means the claim is false against an authority — "1st Edition" on a
    set that never printed one. No rewording makes that true, so we skip the
    repair attempt entirely and save ~700 ms. REPAIRABLE means the underlying
    fact exists but the sentence overstates or fails to cite it ("plenty left"
    at qty=3), which one bounded retry can genuinely fix.
    """

    UNREPAIRABLE = "unrepairable"
    REPAIRABLE = "repairable"


class Verdict(StrEnum):
    PASS = "pass"
    REPAIRED = "repaired"    # failed once, retried, cleared
    BLOCKED = "blocked"      # shown to the operator WITH its reason (D-23)


class LotFormat(StrEnum):
    """How the item is sold. Load-bearing, not cosmetic — see primer §6.

    On a BIN lot a price claim checks against one exact number. On an AUCTION lot
    "price" is four live values that can move between evidence assembly and
    verification, which is the concrete trigger for D-09's staleness re-read.
    Price and quantity writes apply to BIN only (D-03).
    """

    BIN = "bin"
    AUCTION = "auction"


class ActionType(StrEnum):
    """The four write actions (D-04)."""

    PUSH_LOT = "push_lot"              # make a lot the active one on the showcase
    SWAP_SHOWCASE = "swap_showcase"    # reorder what's queued
    MARKDOWN = "markdown"              # BIN only; floor-price guard
    ADJUST_QUANTITY = "adjust_quantity"  # BIN only; oversell guard


class AutomationLevel(StrEnum):
    """The copilot-to-automation ladder (D-22). Per intent class, not global.

    The invariant: the level changes who presses send. It never changes whether
    verification happens.
    """

    OBSERVE = "L0"           # surfaced, no draft
    SUGGEST = "L1"           # draft + human send (default)
    AUTO_SEND = "L2"         # passing drafts send themselves
    AUTO_REVERSIBLE = "L3"   # + reversible actions
    AUTO_CONSEQUENTIAL = "L4"  # never in v1


# =====================================================================
# Evidence and claims — the centre of the system
# =====================================================================


@dataclass(frozen=True)
class Fact:
    """One checkable thing, fetched before generation (D-09).

    `ttl_s` exists because an evidence snapshot goes stale *inside the request*:
    roughly 800 ms passes between assembly and verification, and on an auction lot
    a bid can land in that window. Facts with a TTL are re-read at verify time;
    a fact that moved is its own violation.
    """

    id: str                      # "f3" — short, because the model pays to cite it
    kind: ClaimType
    subject: str                 # "lot_412" | "item_cp_074"
    value: Any
    authority: Authority
    source: str                  # "lots.price" | "psa_pop" | "policy.returns#2"
    as_of: datetime
    ttl_s: int | None = None     # None = stable for the life of the request
    note: str = ""               # human-readable, shown on the trust chip


@dataclass(frozen=True)
class Evidence:
    """The model's entire universe for one draft.

    The prompt states that nothing outside this set is knowable. Verification
    then costs a dict lookup per claim instead of a network call, which is what
    makes safety affordable inside the latency budget (D-33).
    """

    id: str
    lot_id: str | None
    facts: tuple[Fact, ...]
    built_at: datetime
    assembly_ms: int = 0

    def by_id(self, fact_id: str) -> Fact | None:
        for f in self.facts:
            if f.id == fact_id:
                return f
        return None

    def of_kind(self, kind: ClaimType) -> tuple[Fact, ...]:
        return tuple(f for f in self.facts if f.kind == kind)


@dataclass(frozen=True)
class Claim:
    """One assertion the model makes, plus the fact it points at (D-10).

    `quote` is the exact substring of the reply the claim backs. Asking for the
    substring rather than character offsets makes this robust — we can check the
    quote actually occurs in the text, and map sentences to claims by containment
    without trusting the model to count characters.
    """

    type: ClaimType
    value: Any
    source_fact_id: str
    quote: str


@dataclass(frozen=True)
class Violation:
    """Why a draft did not pass. Shown to the operator, never swallowed (D-23)."""

    code: str                    # "variant_not_printed" | "comp_sample_too_small" | ...
    severity: Severity
    message: str                 # operator-facing, plain language
    claim_index: int | None = None
    expected: Any = None
    actual: Any = None


# =====================================================================
# The chat side
# =====================================================================


@dataclass(frozen=True)
class ChatMessage:
    """One raw message off the stream.

    The viewer flags are not decoration: the backpressure drop policy must never
    shed a message from someone with an order in flight, however noisy the room.
    """

    id: str
    ts: datetime
    user_id: str
    display_name: str
    text: str
    has_open_order: bool = False
    is_first_time: bool = False
    prior_purchases: int = 0


@dataclass
class TriagedMessage:
    """A message after the cascade (D-15).

    `route` records which arm answered — "fast" for the deterministic scorer,
    "escalated" when the ambiguous band went to a model. The bench reports the
    two separately because they have different budgets (50 ms vs 600 ms).
    """

    message: ChatMessage
    intent: Intent
    confidence: float
    route: str                   # "fast" | "escalated"
    entities: dict[str, Any] = field(default_factory=dict)
    cluster_key: str = ""
    priority: float = 0.0
    latency_ms: int = 0
    dropped_reason: str | None = None


@dataclass
class QuestionCard:
    """Near-duplicates collapsed into one thing the operator acts on.

    `count` is half the value of the queue: two hundred people asking about
    shipping is one card that says 200, not two hundred cards.
    """

    id: str
    intent: Intent
    representative_text: str
    cluster_key: str
    count: int
    asker_ids: list[str]
    first_seen: datetime
    last_seen: datetime
    priority: float
    lot_id: str | None = None
    draft: "Draft | None" = None
    status: str = "open"         # open | sent | dismissed | escalated


@dataclass
class Draft:
    """A candidate reply, before it reaches the buyer.

    It is called a draft because it is never automatically the message:
        generated -> verified -> (pass | repaired | blocked)
                  -> presented -> (sent | edited | dismissed)

    At AUTO_SEND, "presented" and "sent" collapse — but it is still a draft
    first, and it still passes through verify(). Nothing skips the verifier.
    """

    id: str
    card_id: str
    text: str
    claims: list[Claim]
    evidence_id: str
    model: str
    attempt: int = 0             # 0, or 1 after a repair
    verdict: Verdict | None = None
    violations: list[Violation] = field(default_factory=list)
    latency_ms: dict[str, int] = field(default_factory=dict)
    fallback_text: str | None = None   # safe template when blocked
    cache_hit: bool = False
    created_at: datetime | None = None


# =====================================================================
# The catalog — see primer §2-§5 for what these fields mean
# =====================================================================


@dataclass(frozen=True)
class Grade:
    """Third-party encapsulation. Swings price 10-100x, so every price statement
    must be grade-matched."""

    grader: str                          # PSA | BGS | CGC | RAW
    value: float | None = None           # 10, 9.5; None when raw
    cert: str | None = None              # required for any grade claim
    subgrades: dict[str, float] | None = None   # BGS: centering/corners/edges/surface
    label: str | None = None             # "Black Label", "Pristine"

    @property
    def is_raw(self) -> bool:
        return self.grader.upper() == "RAW"


@dataclass(frozen=True)
class Item:
    """The card (or garment) itself — what this copy actually is."""

    id: str
    game: str                    # "pokemon" | "apparel"
    set_code: str
    number: str                  # "074/073"
    name: str
    language: str                # "en" | "ja"
    finish: str                  # holo | reverse_holo | non_holo | secret_rare | ...
    variants: tuple[str, ...] = ()       # what THIS copy has: ("1st_edition",)
    grade: Grade = Grade("RAW")
    condition: str | None = None         # raw only: NM | LP | MP | HP | DMG
    notes: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)  # fashion slice


@dataclass(frozen=True)
class CardSet:
    """The print-run authority. This is the table that catches the demo case.

    `variants_printed` is keyed by LANGUAGE because the rule is language-scoped:
    English sets stopped carrying a 1st Edition stamp with the EX series in 2003,
    while Japanese printings continued much longer (primer §3). Champion's Path
    has no "1st_edition" entry under "en", so the claim is false by construction
    rather than by judgement.
    """

    code: str
    name: str
    era: str
    released: str                        # ISO date
    variants_printed: dict[str, tuple[str, ...]]   # language -> variants that exist
    has_reverse_holo: bool = True


@dataclass(frozen=True)
class Comp:
    """One sold comparable. Never quotable alone — see primer §5.

    A comp claim requires n >= 5 within the recency window, grade-matched, and
    renders as a range with its sample size.
    """

    item_key: str                # "cp:074/073:PSA10"
    price: float
    sold_at: datetime
    source: str                  # "ebay_sold" | "tcgplayer" | "psa_apr"


@dataclass(frozen=True)
class Policy:
    """A citable clause. Policy claims must point at one of these."""

    id: str                      # "returns#2"
    topic: str                   # returns | shipping | combined_shipping | authenticity
    text: str
    min_item_value: float | None = None   # authenticity gating (primer §6.3)


@dataclass
class Lot:
    """One unit being sold — whatever is on camera now.

    BIN fields and auction fields are both present because a show is usually
    mixed; `format` says which set is authoritative. Writes are format-gated:
    markdown is meaningless on an auction, quantity on a single-card lot (D-03).
    """

    id: str
    listing_id: str
    item_id: str
    title: str
    format: LotFormat
    status: str = "queued"       # queued | live | sold | ended
    position: int = 0
    # BIN
    price: float | None = None
    quantity: int = 0
    floor_price: float | None = None     # markdown guard
    cost_basis: float | None = None
    # auction
    starting_bid: float | None = None
    current_bid: float | None = None
    reserve: float | None = None
    ends_at: datetime | None = None
    # --- auction endgame, for moment detection (D-26b) ------------------
    # Extension count measures the CONTESTED CLOSE rather than total demand:
    # observed lot 9 took 22 bids across 11 extensions (half landed late),
    # lot 6 took 16 bids across 1 (6% late). Similar interest, opposite
    # endgames, and only this number separates them.
    extensions: int = 0
    bid_at_first_extension: float | None = None


# =====================================================================
# The write side
# =====================================================================


@dataclass
class LedgerEntry:
    """One write action's whole life (D-21).

    propose -> precondition snapshot -> confirm -> execute (idempotency key)
            -> read-back verify -> journal with inverse op

    `inverse` is recorded at journal time rather than derived later, because
    deriving a compensating action after the fact needs state we may no longer
    have. Compensating actions, not "undo".
    """

    id: str
    idempotency_key: str
    action: ActionType
    params: dict[str, Any]
    preconditions: dict[str, Any]        # snapshot taken at propose time
    status: str = "proposed"             # proposed|confirmed|executed|verified|failed|rolled_back
    inverse: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime | None = None
    executed_at: datetime | None = None
