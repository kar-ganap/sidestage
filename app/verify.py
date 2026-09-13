"""The verifier. Nothing reaches a buyer without passing through here.

The mechanism, in one line: **a claim cites a fact, and we check the claim
against the fact it cited.**

That last clause is doing work. The model picks inconsistently between two facts
that could both back an assertion (BUILD-LOG B-04), so checking against the
*strongest available* fact would pass a mis-cited claim — true assertion, wrong
evidence, correct-looking reply, and a ledger that records a verification that
never happened. Invisible failures are the expensive ones.

Five passes, in order:

  1. STRUCTURAL   the cited fact exists; the claim's quote is really in the reply
  2. PER-TYPE     `REGISTRY[claim.type]` — one small pure function each
  3. COVERAGE     every asserting sentence is covered by some claim (D-11)
  4. LEXICAL      banned patterns from `policies.json`
  5. STALENESS    any TTL'd fact that moved since assembly (D-09)

Pass 3 is what makes the system fail closed. Passes 1, 2, 4 and 5 only catch
things somebody thought of; pass 3 catches a sentence asserting something with no
claim over it at all, whatever the claim types happen to be.

Adding a rule is a ten-line function and one dict entry. That is deliberate — it
is the extension point, and the registry being pure functions is why a new rule
cannot accidentally depend on anything but its own claim and its own fact.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

from app.catalog import Catalog, get_catalog
from app.evidence import refresh
from app.models import (
    Authority,
    Claim,
    ClaimType,
    Draft,
    Evidence,
    Fact,
    Lot,
    LotFormat,
    Severity,
    Verdict,
    Violation,
)

# --- assertion detection, for the coverage backstop -------------------

_NUMBER = re.compile(r"\d")
_SUPERLATIVE = re.compile(
    r"\b(best|rarest|cleanest|perfect|flawless|mint|gem|pristine|immaculate|"
    r"finest|only|never|always|guaranteed|certainly|definitely)\b", re.I)
_COMMITMENT = re.compile(
    r"\b(will|we'll|i'll|shall|promise|guarantee|refund|replace|ship|"
    r"deliver|honou?r|cover)\b", re.I)
_SENTENCE = re.compile(r"[^.!?]+[.!?]?")

# Quantifiers that assert stock without a number.
_UNBOUNDED = re.compile(
    r"\b(plenty|tons?|loads?|lots|heaps|stacks|as many as you want|"
    r"more than enough)\b", re.I)


@dataclass
class VerifyContext:
    evidence: Evidence
    catalog: Catalog
    lot: Lot | None
    reply: str


@dataclass
class VerifyResult:
    verdict: Verdict
    violations: list[Violation] = field(default_factory=list)
    claims_checked: int = 0
    latency_ms: int = 0

    @property
    def repairable(self) -> bool:
        """Retry only when EVERY violation could survive a rewrite.

        One unrepairable violation poisons the batch: no rewording makes a false
        claim true, so a retry burns ~2.3 s and blocks anyway (D-10b).
        """
        return bool(self.violations) and all(
            v.severity is Severity.REPAIRABLE for v in self.violations)

    def feedback(self) -> str:
        """What to hand back on a repair attempt. Specific, imperative, no
        argument — the model does not need persuading, it needs the rule."""
        lines = [f"- {v.message}" for v in self.violations]
        return "\n".join(lines)


# =====================================================================
# Entry point
# =====================================================================


def verify(draft: Draft, evidence: Evidence, *, catalog: Catalog | None = None,
           now: datetime | None = None) -> VerifyResult:
    cat = catalog or get_catalog()
    now = now or datetime.now(UTC)
    t0 = time.perf_counter()
    lot = cat.lots.get(evidence.lot_id) if evidence.lot_id else None
    ctx = VerifyContext(evidence=evidence, catalog=cat, lot=lot, reply=draft.text)
    v: list[Violation] = []

    for i, claim in enumerate(draft.claims):
        v += _structural(i, claim, ctx)
        fact = evidence.by_id(claim.source_fact_id)
        if fact is None:
            continue                    # already reported; no fact to check against
        checker = REGISTRY.get(claim.type)
        if checker:
            v += [_stamp(x, i) for x in checker(claim, fact, ctx)]

    v += _coverage(draft, ctx)
    v += _lexical(draft, ctx)
    v += _operator_only(draft, ctx)
    v += _staleness(evidence, cat, now)

    v = _dedupe(v)
    verdict = Verdict.BLOCKED if v else Verdict.PASS
    return VerifyResult(verdict=verdict, violations=v, claims_checked=len(draft.claims),
                        latency_ms=int((time.perf_counter() - t0) * 1000))


def _dedupe(violations: list[Violation]) -> list[Violation]:
    """One reason per problem, keeping the most severe.

    The registry and the lexical pass overlap by design — a rule worth encoding
    structurally is usually worth catching textually too, and belt-and-braces is
    correct. But the operator sees this list and reads it aloud under time
    pressure, so two rows describing one fault is a defect in the product even
    though the verdict is right. Severity is kept because it drives the repair
    decision: a repairable duplicate must not mask an unrepairable original.
    """
    best: dict[tuple[str, int | None], Violation] = {}
    for v in violations:
        key = (v.code, v.claim_index)
        prior = best.get(key)
        if prior is None or (prior.severity is Severity.REPAIRABLE
                             and v.severity is Severity.UNREPAIRABLE):
            best[key] = v
    return list(best.values())


def _stamp(v: Violation, i: int) -> Violation:
    return Violation(code=v.code, severity=v.severity, message=v.message,
                     claim_index=i, expected=v.expected, actual=v.actual)


# =====================================================================
# 1 · Structural
# =====================================================================


def _structural(i: int, claim: Claim, ctx: VerifyContext) -> list[Violation]:
    out: list[Violation] = []
    if ctx.evidence.by_id(claim.source_fact_id) is None:
        out.append(Violation(
            code="fact_not_found", severity=Severity.UNREPAIRABLE, claim_index=i,
            message=(f"claim {i} cites {claim.source_fact_id!r}, which is not in the "
                     f"evidence. Cite only the fact ids you were given."),
            actual=claim.source_fact_id))

    # A quote that is not in the reply covers nothing — the claim looks like it
    # backs a sentence and does not, which defeats the coverage pass silently.
    q = claim.quote.strip()
    if not q:
        out.append(Violation(
            code="quote_empty", severity=Severity.REPAIRABLE, claim_index=i,
            message=f"claim {i} has no quote. Quote the exact words it backs."))
    elif _norm(q) not in _norm(ctx.reply):
        out.append(Violation(
            code="quote_not_in_reply", severity=Severity.REPAIRABLE, claim_index=i,
            message=(f"claim {i} quotes {q!r}, which does not appear in the reply. "
                     f"The quote must be an exact substring."),
            actual=q))
    elif _sentence_count(q) > 1:
        # BUILD-LOG B-09: the model bundles several assertions under one claim.
        # One fact then stands in for two statements, and only one gets checked.
        out.append(Violation(
            code="quote_spans_sentences", severity=Severity.REPAIRABLE, claim_index=i,
            message=(f"claim {i} covers more than one sentence. Emit one claim per "
                     f"assertion so each is checked against its own fact."),
            actual=q))
    return out


# =====================================================================
# 2 · Per-type registry
# =====================================================================

Verifier = Callable[[Claim, Fact, VerifyContext], list[Violation]]


def _variant(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """The demo case, and the most nuanced rule in the registry.

    Whether a fact supports a variant claim depends on which authority it has:

    - CATALOG (what the set ever printed) can prove a variant IMPOSSIBLE, and can
      prove a card IS variant V only when the print run is a singleton — if a set
      offered three variants, knowing the set tells you nothing about this copy.
    - RECORD (what this copy carries) proves what this copy is, and cannot speak
      to what other copies could be.
    """
    asserted = _slug(claim.value)
    negated = _is_negated(claim.quote)

    if fact.authority is Authority.CATALOG:
        printed = [_slug(p) for p in fact.value.get("printed", ())]
        lang = fact.value.get("language", "en")
        if asserted not in printed:
            if negated:
                return []            # "not 1st edition" + never printed = correct
            return [Violation(
                code="variant_not_printed", severity=Severity.UNREPAIRABLE,
                message=(f"{fact.note} — so {claim.value!r} was never printed in "
                         f"{lang}. This cannot be said by rewording."),
                expected=printed, actual=claim.value)]
        if len(printed) > 1 and not negated:
            return [Violation(
                code="variant_indeterminate", severity=Severity.REPAIRABLE,
                message=(f"the set printed {printed}, so a set-level fact cannot say "
                         f"which one THIS copy is. Cite the record fact for this copy."),
                expected=printed, actual=claim.value)]
        return []

    if fact.authority is Authority.RECORD:
        # BUILD-LOG B-11. `reverse_holo` and `holo` are FINISHES, not variants —
        # different columns on the item. Treating a finish as a missing variant
        # blocked "is that pikachu a reverse holo?" on a card whose finish is
        # exactly that. The record fact carries only variants, so a finish claim
        # is a mis-citation rather than a falsehood: point at the identity fact.
        if asserted in _FINISHES:
            return [] if negated else [_miscite(
                claim, fact, "finish is on the identity fact, not the variant fact")]
        on_copy = [_slug(x) for x in fact.value.get("variants", ())]
        if negated:
            return [] if asserted not in on_copy else [Violation(
                code="variant_wrongly_denied", severity=Severity.REPAIRABLE,
                message=f"this copy IS {claim.value!r} — {fact.note}",
                expected=on_copy, actual=claim.value)]
        if asserted not in on_copy:
            return [Violation(
                code="variant_not_on_copy", severity=Severity.UNREPAIRABLE,
                message=f"{fact.note} — this copy is not {claim.value!r}.",
                expected=on_copy, actual=claim.value)]
        return []

    return [_miscite(claim, fact, "a variant claim needs a record or catalog fact")]


def _comp(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """Never a bare number. Primer §5.

    The fact already carries `quotable` and the sanctioned `phrase`; this rule
    only has to notice that the reply ignored them.
    """
    if fact.kind is not ClaimType.COMP:
        return [_miscite(claim, fact, "a comp claim must cite a comp fact")]
    if not fact.value.get("quotable"):
        # The message below prescribes declining. A reply that declines is
        # therefore correct, and blocking it made the instruction impossible to
        # follow (B-24). A *number* alongside the denial is still a violation.
        if _asserts_absence(claim) and not _numbers(claim.quote):
            return []
        return [Violation(
            code="comp_not_quotable", severity=Severity.UNREPAIRABLE,
            message=(f"no quotable comp: {fact.value.get('reason')}. Say we do not "
                     f"have enough recent sales rather than giving a number."),
            expected=fact.value.get("reason"), actual=claim.value)]

    phrase = fact.value.get("phrase", "")
    if _norm(phrase) in _norm(ctx.reply):
        return []                        # the sanctioned rendering was used verbatim

    lo, hi = fact.value.get("low"), fact.value.get("high")
    nums = _numbers(claim.quote)
    if len(nums) < 2:
        return [Violation(
            code="bare_comp", severity=Severity.REPAIRABLE,
            message=(f"comps render as a range with their sample size. Use exactly: "
                     f"{phrase!r}"),
            expected=phrase, actual=claim.quote)]
    if min(nums) < lo * 0.98 or max(nums) > hi * 1.02:
        return [Violation(
            code="comp_outside_range", severity=Severity.REPAIRABLE,
            message=f"quoted range is not the recorded one. Use exactly: {phrase!r}",
            expected=(lo, hi), actual=(min(nums), max(nums)))]
    return []


def _pop(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """Population figures are third-party and time-varying, so an as-of date is
    part of the claim rather than a nicety (primer §4)."""
    if fact.kind is not ClaimType.POP:
        return [_miscite(claim, fact, "a pop claim must cite a population fact")]
    if fact.value.get("stale"):
        return [Violation(
            code="pop_stale", severity=Severity.UNREPAIRABLE,
            message=(f"this population figure was read "
                     f"{fact.value.get('age_days')} days ago and is stale."),
            actual=fact.value.get("as_of"))]
    as_of = str(fact.value.get("as_of", ""))
    if as_of and as_of not in ctx.reply and _yearless(as_of) not in ctx.reply:
        return [Violation(
            code="pop_missing_as_of", severity=Severity.REPAIRABLE,
            message=(f"a population figure must carry the date it was read "
                     f"({as_of}). Pop moves."),
            expected=as_of)]
    return []


# Words that mark a claim as asserting the ABSENCE of something rather than its
# value. Kept narrow on purpose: this predicate can only ever turn a violation
# into a pass, so a loose pattern is a hole in the verifier.
_DENIAL = re.compile(
    r"\b(no|not|none|never|without|un\w+ed|isn.?t|aren.?t|don.?t|doesn.?t|"
    r"can.?t|cannot|lack\w*|zero|n/?a|raw|ungraded|unavailable|insufficient|"
    r"unknown)\b", re.I)


def _asserts_absence(claim: Claim) -> bool:
    """Is this claim saying "there is no X" rather than "X is 7"?

    B-24. Two verifiers independently had the same bug: a fact recording that
    something is ABSENT — an ungraded card, a comp window with too few sales —
    was used to block a reply that correctly said so. `_comp`'s violation
    message literally reads "Say we do not have enough recent sales rather than
    giving a number", and it fired on a reply that said exactly that.

    A claim asserting absence is *supported* by the fact recording the absence.
    The two are the same statement. Checking it as though it asserted presence
    is the error, and it makes the system's own prescribed answer unsendable.
    """
    return bool(_DENIAL.search(claim.value) or _DENIAL.search(claim.quote))


def _grade(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if fact.kind is not ClaimType.GRADE:
        return [_miscite(claim, fact, "a grade claim must cite a grade fact")]
    if fact.value.get("raw"):
        # A numeric grade on a raw card is the violation. Saying it is ungraded
        # is the correct answer, and the record is what establishes it (B-24).
        stated = [n for n in (_numbers(claim.value) or _numbers(claim.quote)) if 0 < n <= 10]
        if not stated and _asserts_absence(claim):
            return []
        return [Violation(
            code="grade_on_raw_card", severity=Severity.UNREPAIRABLE,
            message="this card is ungraded — there is no grade to state.",
            actual=claim.value)]
    if not fact.value.get("cert"):
        return [Violation(
            code="grade_cert_missing", severity=Severity.UNREPAIRABLE,
            message="a grade claim needs a cert number; this record has none.")]
    # BUILD-LOG B-10. A cert number is an eight-digit integer sitting in the same
    # sentence as the grade, and a naive "first number in the quote" read treats
    # cert 71004412 as a claimed grade of 71004412. Found by the B2 control set on
    # "cert on the blastoise?" — a benign question that was being over-blocked.
    # Grades live in 1..10, so anything outside that range is not a grade claim.
    stated = [n for n in (_numbers(claim.value) or _numbers(claim.quote)) if 0 < n <= 10]
    actual = fact.value.get("value")
    if stated and actual is not None and abs(stated[0] - float(actual)) > 1e-6:
        return [Violation(
            code="grade_mismatch", severity=Severity.UNREPAIRABLE,
            message=f"the record says {fact.value['grader']} {actual:g}.",
            expected=actual, actual=stated[0])]
    return []


def _condition(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """D-12. Asked five times in 477 observed messages, answerable zero times.

    An observational fact exists precisely so the model can decline while
    pointing at something. Citing it to ASSERT is the error.
    """
    if fact.authority is Authority.OBSERVATIONAL:
        if _is_deferral(claim.quote):
            return []
        return [Violation(
            code="observational_assertion", severity=Severity.UNREPAIRABLE,
            message=(f"{', '.join(fact.value.get('observational', []))} can only be "
                     f"judged from the card in hand. Say the host will check on camera."),
            expected="defer to host", actual=claim.quote)]
    return []


def _centering(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """Only assertable from a grader subgrade. On a PSA slab centring is merely
    implied by the grade; on a raw card it is observational."""
    if fact.kind is not ClaimType.CENTERING or fact.authority is not Authority.RECORD:
        return [Violation(
            code="centering_no_subgrade", severity=Severity.UNREPAIRABLE,
            message=("centring is only stateable from a grader's subgrade. This record "
                     "has none — defer to the host."))]
    return []


def _availability(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if _UNBOUNDED.search(claim.quote):
        qty = fact.value.get("quantity")
        return [Violation(
            code="unbounded_quantifier", severity=Severity.REPAIRABLE,
            message=(f"unbounded quantity language is unverifiable. State the number"
                     + (f" — {qty} available." if qty is not None else ".")),
            expected=qty, actual=claim.quote)]
    stated = _numbers(claim.value)
    qty = fact.value.get("quantity")
    if stated and qty is not None and int(stated[0]) != int(qty):
        return [Violation(
            code="availability_mismatch", severity=Severity.REPAIRABLE,
            message=f"the record says {qty} available.", expected=qty, actual=stated[0])]
    return []


def _price(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if fact.value.get("operator_only"):
        return [Violation(
            code="operator_only_leaked", severity=Severity.UNREPAIRABLE,
            message=("that figure is the seller's reserve and is for their eyes only. "
                     "Saying it destroys their position."),
            actual=claim.value)]
    stated = _numbers(claim.value)
    recorded = fact.value.get("price") or fact.value.get("closed_at")
    if stated and recorded is not None and abs(stated[0] - float(recorded)) > 0.01:
        return [Violation(
            code="price_mismatch", severity=Severity.REPAIRABLE,
            message=f"the record says ${float(recorded):,.2f}.",
            expected=recorded, actual=stated[0])]
    return []


def _bid(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    stated = _numbers(claim.value)
    current = fact.value.get("current_bid")
    if stated and current is not None and abs(stated[0] - float(current)) > 0.01:
        return [Violation(
            code="bid_mismatch", severity=Severity.REPAIRABLE,
            message=f"the current bid is ${float(current):,.2f}.",
            expected=current, actual=stated[0])]
    return []


def _policy(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """A policy claim must cite a policy clause, not paraphrase common sense.

    Observed live: a seller announced "all auction sales are final" on stream,
    which may conflict with platform buyer protection. Repeating an unenforceable
    disclaimer would manufacture liability for our own user.
    """
    if fact.authority is not Authority.RECORD or "clause" not in (fact.value or {}):
        return [Violation(
            code="policy_uncited", severity=Severity.UNREPAIRABLE,
            message=("state policy only from a policy fact. Never from what sounds "
                     "standard, and never from what the seller said on stream."),
            actual=claim.value)]
    gate = fact.value.get("min_item_value")
    if gate is not None and ctx.lot is not None:
        # BUILD-LOG B-11. The naive read used current_bid, which is None on a
        # QUEUED auction lot that has not opened — so value fell to 0 and every
        # value-gated clause failed. B2 caught it on "when does it ship?", a
        # question with nothing wrong with it. A lot's value is estimated from
        # the best figure available, and when none exists the gate does not fire:
        # refusing to answer because we cannot price the lot is over-blocking.
        value = _lot_value(ctx.lot)
        if value is not None and value < gate:
            return [Violation(
                code="authenticity_value_gate", severity=Severity.UNREPAIRABLE,
                message=(f"{fact.value['clause']} applies at ${gate:,.0f} and above; "
                         f"this lot is at ${value:,.0f}."),
                expected=gate, actual=value)]
    return []


REGISTRY: dict[ClaimType, Verifier] = {
    ClaimType.VARIANT: _variant,
    ClaimType.COMP: _comp,
    ClaimType.POP: _pop,
    ClaimType.GRADE: _grade,
    ClaimType.CONDITION: _condition,
    ClaimType.CENTERING: _centering,
    ClaimType.AVAILABILITY: _availability,
    ClaimType.PRICE: _price,
    ClaimType.BID: _bid,
    ClaimType.SHIPPING: _policy,
    ClaimType.RETURNS: _policy,
    ClaimType.AUTHENTICITY: _policy,
}


def _miscite(claim: Claim, fact: Fact, why: str) -> Violation:
    """B-04. The assertion may well be true; the evidence named does not
    establish it, and a ledger that records otherwise is worse than a block."""
    return Violation(
        code="mis_citation", severity=Severity.REPAIRABLE,
        message=f"{claim.source_fact_id} is a {fact.kind.value} fact — {why}.",
        expected=fact.kind.value, actual=claim.type.value)


# =====================================================================
# 3 · Coverage — the backstop that makes it fail closed
# =====================================================================


def _coverage(draft: Draft, ctx: VerifyContext) -> list[Violation]:
    """Any sentence that asserts something must be covered by a claim.

    This is D-11, and it is the only pass that catches a claim type nobody
    enumerated. Everything above only finds what somebody anticipated.
    """
    covered = [_norm(c.quote) for c in draft.claims if c.quote.strip()]
    out: list[Violation] = []
    for raw in _SENTENCE.findall(draft.text):
        s = raw.strip()
        if not s or not _asserts(s):
            continue
        n = _norm(s)
        if any(q and (q in n or n in q) for q in covered):
            continue
        out.append(Violation(
            code="unbacked_claim", severity=Severity.REPAIRABLE,
            message=(f"nothing backs {s!r}. Every sentence with a number, a "
                     f"superlative or a commitment needs a claim citing a fact."),
            actual=s))
    return out


def _asserts(sentence: str) -> bool:
    return bool(_NUMBER.search(sentence) or _SUPERLATIVE.search(sentence)
                or _COMMITMENT.search(sentence))


# =====================================================================
# 4 · Lexical — deterministic, regardless of citation
# =====================================================================


def _lexical(draft: Draft, ctx: VerifyContext) -> list[Violation]:
    """Patterns from `policies.json`. These fire even on a well-cited claim —
    some phrasings are wrong however well evidenced the underlying fact is."""
    out: list[Violation] = []
    for rule in ctx.catalog.banned_claims:
        if re.search(rule["pattern"], draft.text, re.I):
            out.append(Violation(
                code=rule["code"],
                severity=(Severity.UNREPAIRABLE if rule["code"] == "investment_advice"
                          else Severity.REPAIRABLE),
                message=rule["message"], actual=draft.text))
    return out


def _operator_only(draft: Draft, ctx: VerifyContext) -> list[Violation]:
    """A reserve can leak without any claim pointing at it — the model may have
    simply read it in the evidence and repeated it. So this checks the text.

    BUILD-LOG B-10. The naive version flagged any occurrence of the reserve
    figure, and a reserve routinely sits INSIDE the legitimate comp range it was
    set from — lot_006's reserve is $1,100 while comps run $1,150–$1,310. A
    quoted range that happens to straddle it is not a leak. So a number is only
    a leak when no other fact in the bundle can account for it.
    """
    legit: set[int] = set()
    for f in ctx.evidence.facts:
        if not isinstance(f.value, dict) or f.value.get("operator_only"):
            continue
        for v in f.value.values():
            if isinstance(v, (int, float)) and v > 0:
                legit.add(int(v))

    for f in ctx.evidence.facts:
        if not (isinstance(f.value, dict) and f.value.get("operator_only")):
            continue
        for k, v in f.value.items():
            if k == "operator_only" or not isinstance(v, (int, float)) or v <= 0:
                continue
            if int(v) in legit:
                continue            # another fact explains this number
            if re.search(rf"\b{int(v):,}\b|\b{int(v)}\b", draft.text):
                return [Violation(
                    code="operator_only_leaked", severity=Severity.UNREPAIRABLE,
                    message=("the reply contains the seller's reserve. That figure is "
                             "never said to the room."),
                    actual=v)]
    return []


# =====================================================================
# 5 · Staleness
# =====================================================================


def _staleness(ev: Evidence, cat: Catalog, now: datetime) -> list[Violation]:
    """A fact that moved between assembly and verification (D-09).

    Not a silent update: the draft was written against the old value and its
    claims may no longer follow from the new one.
    """
    return [Violation(
        code="stale_evidence", severity=Severity.REPAIRABLE,
        message=(f"{s.fact.kind.value} moved from {s.was} to {s.now} while this was "
                 f"being drafted. Re-draft against the current value."),
        expected=s.now, actual=s.was)
        for s in refresh(ev, catalog=cat, now=now)]


# =====================================================================
# helpers
# =====================================================================


_FINISHES = frozenset({
    "holo", "reverse_holo", "non_holo", "secret_rare", "ultra_rare", "promo",
    "shining", "full_art", "alt_art", "rainbow_rare", "gold",
})


def _lot_value(lot: Lot) -> float | None:
    """Best available estimate of what a lot is worth, or None if unknowable.

    Order matters: a live BIN price or current bid is what the lot IS worth; a
    starting bid or reserve is what it is expected to be worth. None means the
    caller must not gate on value at all (B-11) — a queued lot that has not
    opened is not evidence of a cheap lot.
    """
    for v in (lot.price, lot.current_bid, lot.reserve, lot.starting_bid):
        if v:
            return float(v)
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s$.,-]", "", s.lower()).strip()


def _slug(v: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_")


def _numbers(s: object) -> list[float]:
    return [float(x.replace(",", "")) for x in
            re.findall(r"\d[\d,]*(?:\.\d+)?", str(s))]


def _sentence_count(s: str) -> int:
    return len([x for x in _SENTENCE.findall(s) if x.strip()])


def _is_negated(quote: str) -> bool:
    return bool(re.search(r"\b(not|isn'?t|wasn'?t|no|never|aren'?t)\b", quote, re.I))


def _is_deferral(quote: str) -> bool:
    return bool(re.search(
        r"\b(host|seller|check|show|camera|look|hold it up|can'?t tell|"
        r"from here|in hand)\b", quote, re.I))


def _yearless(iso: str) -> str:
    parts = iso.split("-")
    return "/".join(parts[1:]) if len(parts) == 3 else iso
