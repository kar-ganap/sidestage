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
from app.config import settings
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
# Inflections matter (B-43). These were uninflected, so `\bship\b` did not
# match "Ships" and `\brefund\b` did not match "refunds" — an uncited
# "Ships same day and refunds are processed immediately" was never even
# examined. Spans and the exemption set are both compared on stems (`_stem`),
# so widening here does not cost a matching failure on the other side.
# B-57. `only`, `never` and `always` came out: they are scope and denial words
# far more often than superlatives here, and "Champion's Path only came out
# unlimited" is the flagship demo's own CORRECT denial — it was blocking as a
# REPAIRABLE `unbacked_claim` instead of the UNREPAIRABLE `variant_not_printed`
# the demo exists to show.
_SUPERLATIVE = re.compile(
    r"\b(best|rarest|cleanest|perfect(?:ly)?|flawless|mint|gem|pristine|"
    r"immaculate|finest|guarantee[sd]?|certainly|definitely|absolutely)\b", re.I)
_COMMITMENT = re.compile(
    r"\b(will|we'll|i'll|shall|promise[sd]?|guarantee[sd]?|refund(?:s|ed)?|"
    r"replace[sd]?|ship(?:s|ped|ping)?|deliver(?:s|ed|y)?|honou?r[sd]?|"
    r"cover[sd]?|final|postage|dispatch(?:es|ed)?|send(?:s|ing)?|tracked|"
    r"free)\b", re.I)

# A sentence ends at . ! or ? — but NOT at a decimal point (B-44). The naive
# `[^.!?]+` split "$890.00" and "BGS 9.5" in two, which fired
# `quote_spans_sentences` on a single true sentence and then demanded a
# citation for the orphan fragment "Current bid is $890.". Half-grades and
# prices with cents are ordinary in this domain, so this was not an edge case.
_SENTENCE = re.compile(r"(?:[^.!?]|(?<=\d)\.(?=\d))+[.!?]?")

# Quantifiers that assert stock without a number.
_UNBOUNDED = re.compile(
    r"\b(plenty|tons?|loads?|lots|heaps|stacks|as many as you want|"
    r"more than enough|a (?:handful|few|couple)|several)"
    # ...but not when the thing being quantified is TIME. B-96: "give me a few
    # seconds", "the host will hold it up in a couple of minutes" and "several
    # people have asked" are the deferral the verifier's own messages prescribe,
    # and all three blocked as unbounded STOCK claims.
    r"(?!\s+(?:of\s+)?(?:seconds?|minutes?|hours?|days?|weeks?|moments?|people|"
    r"viewers?|folks|others?|times?))\b", re.I)

# Numbers written as words. `_NUMBER` only ever saw digits, so "We have twenty
# of these left" against a quantity of zero was not an assertion at all (B-43).
# "one" is deliberately absent: in this domain it is a pronoun ("this one",
# "that one") far more often than a quantity, and including it demanded a
# citation for the most common way a seller refers to the item in front of them.
_NUMWORD = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"twenty|thirty|forty|fifty|hundred|thousand|dozen)\b", re.I)


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
    ctx = VerifyContext(evidence=evidence, catalog=cat, lot=lot,
                        reply=draft.text)
    v: list[Violation] = []

    for i, claim in enumerate(draft.claims):
        v += _structural(i, claim, ctx)
        fact = evidence.by_id(claim.source_fact_id)
        if fact is None:
            continue                    # already reported; no fact to check against
        checker = REGISTRY.get(claim.type)
        if checker is None:
            # FAIL CLOSED (B-37). `REGISTRY.get(...)` returning None used to mean
            # "skip", so `identity` and `sizing` — both offered to the model in
            # DRAFT_SYSTEM's claim-type list — went completely unchecked. One
            # `identity` claim could launder a fabricated grade, a false bid and
            # a shipping promise, because coverage was satisfied by the quote and
            # no per-type pass ever ran. `identity` is the second most common
            # claim type in the recorded fixtures.
            v.append(_stamp(Violation(
                code="unverifiable_claim_type", severity=Severity.UNREPAIRABLE,
                message=(f"claim type {claim.type.value!r} has no verifier, so "
                         f"nothing can check it. Do not assert it."),
                actual=claim.value), i))
            continue
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
    best: dict[tuple[str, int | None, str], Violation] = {}
    for v in violations:
        # B-51. The key was `(code, claim_index)` and every coverage violation
        # carries `claim_index=None`, so three independently unbacked sentences
        # collapsed into one message about the first. The bounded retry fixed
        # that sentence and blocked again on the next — a repair loop that can
        # only ever make one pass of progress. `actual` is the sentence for
        # coverage and the claim text elsewhere, which is exactly the
        # discriminator this needs.
        key = (v.code, v.claim_index, str(v.actual)[:80])
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


_WORD_ONLY = re.compile(r"[a-z]{2,}", re.I)


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
    # B-37. `_require_kind`'s first docstring asserted that `_variant` already
    # guarded. It did not — it branched on `fact.authority` alone, so a
    # variant claim citing ANY record fact read `.get("variants", ())` off
    # the wrong fact and passed. "It is not a 1st edition" verified clean
    # against a record saying it is.
    if (v := _require_kind(claim, fact, ClaimType.VARIANT,
                           "a variant claim must cite a variant fact")):
        return v
    asserted = _slug(claim.value)
    # B-46. This read `_is_negated(claim.quote)` — the WHOLE quote. So the most
    # natural phrasing in the domain, affirming one variant while denying
    # another, disarmed the flagship unrepairable case:
    #
    #     "This copy is 1st Edition."                 -> BLOCKED (correct)
    #     "This copy is 1st Edition, not Shadowless." -> PASS    (the same lie)
    #
    # `\bno\b` inside "no doubt" did it too. Negation has to be read where the
    # claimed value actually sits, not anywhere in the sentence.
    negated = _denies(claim.quote, claim.value)         # B-46, B-94

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
    # B-61. This read `ctx.reply`, so ONE claim rendering the sanctioned phrase
    # exempted every OTHER comp claim in the draft: "Last 5 sold $305–$370, past
    # 90d. Honestly these go for $900 all day." passed against a 305–370 record.
    if phrase and _norm(phrase) in _norm(claim.quote):
        return []                        # this claim used the sanctioned rendering

    lo, hi = fact.value.get("low"), fact.value.get("high")
    # B-52. This read every number in the quote, so "last 5 sold ... past 90
    # days" contributed 5 and 90 as though they were prices and `min(nums)`
    # fired `comp_outside_range` on any rewording of the sanctioned phrase.
    # Only money-shaped figures are prices.
    nums = _money(claim.quote) or _numbers(claim.quote)
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


def _require_kind(claim: Claim, fact: Fact, kind: ClaimType, what: str
                  ) -> list[Violation] | None:
    """B-33. Every verifier must check the fact it was handed is the right kind.

    Four of the twelve did. Eight did not — `_condition`, `_centering`,
    `_availability`, `_price`, `_bid` and the three policy types went straight
    to `fact.value.get(...)`, got `None` from a fact of the wrong kind, and
    returned `[]`. A pass.

    That is B-04's mis-citation hole, wide open, on the mechanism this project
    leads with. Reproduced before fixing: a `bid` claim of "$4" citing the
    *identity* fact, against a lot whose real bid is $890, verified clean.

    It survived because `_dedupe`, `_structural` and `_coverage` all looked
    healthy and the registry dispatched correctly — the missing check was four
    lines that were simply never written in six of the functions, and no test
    asserted that a claim must cite its own kind.
    """
    if fact.kind is not kind:
        return [_miscite(claim, fact, what)]
    return None


def _identity(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """Is this actually the card we are looking at? (B-37)

    `identity` was in `ClaimType` and offered to the model, but had no entry in
    `REGISTRY` — so `REGISTRY.get()` returned None and the claim was skipped
    entirely. It is the second most common claim type in the recorded fixtures,
    which made it the widest hole in the contract: one identity claim quoting a
    whole sentence satisfied coverage while nothing checked a word of it.

    The check is token containment rather than string equality, because the
    model writes "the Base Set Charizard 4/102" where the record holds the same
    facts in separate fields. Every content word it asserts must appear in the
    record; a fabricated name or number has a token that does not.
    """
    if (v := _require_kind(claim, fact, ClaimType.IDENTITY,
                           "an identity claim must cite an identity fact")):
        return v
    # B-48. When the reference is ambiguous, `assemble` mints an identity fact
    # holding the CANDIDATES rather than an identity — D-14's whole mechanism,
    # so the model can cite the question instead of guessing. Token containment
    # then rejected the clarifier for naming the candidates properly, and did it
    # UNREPAIRABLY, so "Which Mew do you mean, the Celebrations 011/025 or the
    # ex?" went straight to the fallback. Asking is not asserting.
    # ...and only when the reply is actually ASKING. B-63: this returned []
    # unconditionally, and because the clarifier fact now carries every
    # candidate's set, number and grade flattened together, "That Celebrations
    # Mew 011/025 is a PSA 9" passed — the Celebrations Mew is RAW; the PSA 9 is
    # the other card.
    if isinstance(fact.value, dict) and fact.value.get("ambiguous"):
        # B-93. Scoped to what this claim QUOTES, not to the whole reply.
        # `"?" in ctx.reply` meant appending a question anywhere turned an
        # answer into a question: "That Celebrations Mew 011/025 is a PSA 9.
        # Want me to grab it for you?" passed, and the Celebrations Mew is RAW
        # — the PSA 9 is the other candidate.
        return [] if "?" in (claim.quote or "") else [Violation(
            code="answered_an_ambiguous_reference", severity=Severity.UNREPAIRABLE,
            message=("the reference is ambiguous — ask which one rather than "
                     "answering about one of them."),
            expected=fact.value.get("question"), actual=claim.value)]
    known = {t for val in fact.value.values()
             for t in re.findall(r"[a-z0-9]+", _norm(str(val)))}
    asserted = re.findall(r"[a-z0-9]+", _norm(claim.value))
    unknown = [t for t in asserted if t not in known and len(t) > 1]
    if unknown:
        return [Violation(
            code="identity_mismatch", severity=Severity.UNREPAIRABLE,
            message=(f"the record does not say {', '.join(repr(u) for u in unknown)}"
                     f" — it is {fact.note or fact.value}."),
            expected=fact.note or str(fact.value), actual=claim.value)]
    return []


def _sizing(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """Apparel sizing, which we hold no authority for.

    D-02 puts the fashion slice here deliberately: far more of a garment's
    attributes are observational than a card's, so the copilot defers more
    often *from the same registry*. No sizing fact is ever minted, so every
    sizing claim mis-cites and blocks — which is the correct answer, arrived at
    by the general rule rather than by a special case.
    """
    if (v := _require_kind(claim, fact, ClaimType.SIZING,
                           "we hold no sizing record; sizing is the seller's to state")):
        # UNREPAIRABLE by D-10b's own argument (B-50): no sizing fact is ever
        # minted, so a retry cannot find one and every sizing question burned
        # the ~2.3 s repair by construction before blocking anyway.
        return [Violation(code=v[0].code, severity=Severity.UNREPAIRABLE,
                          message=v[0].message, expected=v[0].expected,
                          actual=v[0].actual)]
    return []


def _grade(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if fact.kind is not ClaimType.GRADE:
        return [_miscite(claim, fact, "a grade claim must cite a grade fact")]
    if fact.value.get("raw"):
        # A numeric grade on a raw card is the violation. Saying it is ungraded
        # is the correct answer, and the record is what establishes it (B-24).
        stated = [n for n in _numbers(claim.value) if 0 < n <= 10]
        # B-69. This fell back to `_numbers(claim.quote)`, so a correct DENIAL
        # whose quote names the grade being denied — "so I can't call it a PSA
        # 9" — supplied its own 9, `stated` became non-empty, `_asserts_absence`
        # was skipped, and the reply blocked UNREPAIRABLY. Widening the quote by
        # three words flipped the severity. A denial is established by the
        # claim's VALUE; the quote is a highlight, not evidence.
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
    stated = [n for n in _numbers(claim.value) if 0 < n <= 10]
    actual = fact.value.get("value")
    if stated and actual is not None and abs(stated[0] - float(actual)) > 1e-6:
        return [Violation(
            code="grade_mismatch", severity=Severity.UNREPAIRABLE,
            message=f"the record says {fact.value['grader']} {actual:g}.",
            expected=actual, actual=stated[0])]
    return []


def _condition(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """D-12. Asked five times in 485 observed messages, answerable zero times.

    An observational fact exists precisely so the model can decline while
    pointing at something. Citing it to ASSERT is the error.
    """
    if (v := _require_kind(claim, fact, ClaimType.CONDITION, "a condition claim must cite a condition fact")):
        return v
    if fact.authority is Authority.OBSERVATIONAL:
        # B-64 widened this from `claim.quote` to `ctx.reply`, because the
        # recorded refusal defers in the sentence AFTER the one it quotes.
        # B-93: reply scope went too far the other way — the model could ASSERT
        # an observational attribute and defer in a later sentence:
        #
        #   "Yes, the back is spotless with sharp corners."            BLOCKED
        #   ...+ " I'll have the host show it on camera too."          PASSED
        #
        # D-12 nullified by appending the very sentence the violation prescribes.
        # The middle ground is the SENTENCE the claim quotes, plus the one after
        # it: enough for a refusal that defers next, not enough to launder an
        # assertion made three sentences earlier.
        if _is_deferral(_near_quote(ctx.reply, claim.quote)):
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
    if (v := _require_kind(claim, fact, ClaimType.CENTERING, "a centering claim must cite a centering fact")):
        return v
    if fact.authority is not Authority.RECORD:
        return [Violation(
            code="centering_no_subgrade", severity=Severity.UNREPAIRABLE,
            message=("centring is only stateable from a grader's subgrade. This record "
                     "has none — defer to the host."))]
    # B-47. Everything above was the whole function, and the condition was dead
    # (centering facts are always RECORD), so ANY value verified clean — a
    # fabricated "60/40" against a recorded 9.5 subgrade passed and was
    # ledgered as checked. The subgrade is a number, not a ratio, so a claim
    # that states a measurement must state one the record contains.
    # B-62. This compared against `_keys` of the whole fact, so any subgrade
    # stood in for centring — a claimed 9.5 passed against a centring of 8.0
    # because 9.5 was the EDGES subgrade. The key the number belonged to is the
    # entire point of a subgrade.
    sub = fact.value if isinstance(fact.value, dict) else {}
    recorded = _keys(sub.get("centering", "")) if "centering" in sub \
        else _keys(f"{fact.value} {fact.note}")
    stated = [sp for sp, _, _ in _assertive_spans(_norm(claim.value))
              if sp not in recorded]
    if stated:
        return [Violation(
            code="centering_mismatch", severity=Severity.UNREPAIRABLE,
            message=(f"the subgrades are {fact.note or fact.value} — they do not "
                     f"say {claim.value!r}. A centring measurement cannot be "
                     f"reworded into existence."),
            expected=fact.note or str(fact.value), actual=claim.value)]
    return []


def _availability(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if (v := _require_kind(claim, fact, ClaimType.AVAILABILITY, "an availability claim must cite an availability fact")):
        return v
    qty = fact.value.get("quantity")
    # B-104. `settings.stock_quantifier_floor` existed with the comment "below
    # this, 'plenty' is a violation" and was read by NOTHING — this pass flagged
    # any unbounded quantifier regardless of stock, so a seller with 500 of
    # something could not say "plenty", which is simply true. A setting whose
    # comment describes behaviour the code does not have is worse than no
    # setting: it tells a reader the rule is configurable when it is not.
    if (_UNBOUNDED.search(ctx.reply)        # B-60: the quote is not the sentence
            and (qty is None or qty < settings.stock_quantifier_floor)):
        return [Violation(
            code="unbounded_quantifier", severity=Severity.REPAIRABLE,
            message=(f"unbounded quantity language is unverifiable. State the number"
                     + (f" — {qty} available." if qty is not None else ".")),
            expected=qty, actual=claim.quote)]
    qty = fact.value.get("quantity")
    if not _states(claim, qty):                 # B-49
        return [Violation(
            code="availability_mismatch", severity=Severity.REPAIRABLE,
            message=f"the record says {qty} available.",
            expected=qty, actual=claim.value)]
    return []


def _price(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if (v := _require_kind(claim, fact, ClaimType.PRICE, "a price claim must cite a price fact")):
        return v
    if fact.value.get("operator_only"):
        return [Violation(
            code="operator_only_leaked", severity=Severity.UNREPAIRABLE,
            message=("that figure is the seller's reserve and is for their eyes only. "
                     "Saying it destroys their position."),
            actual=claim.value)]
    recorded = fact.value.get("price") or fact.value.get("closed_at")
    if not _states(claim, recorded):            # B-49, not `_numbers(...)[0]`
        return [Violation(
            code="price_mismatch", severity=Severity.REPAIRABLE,
            message=f"the record says ${float(recorded):,.2f}.",
            expected=recorded, actual=claim.value)]
    return []


def _bid(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    if (v := _require_kind(claim, fact, ClaimType.BID, "a bid claim must cite a bid fact")):
        return v
    current = fact.value.get("current_bid")
    if not _states(claim, current):             # B-49
        return [Violation(
            code="bid_mismatch", severity=Severity.REPAIRABLE,
            message=f"the current bid is ${float(current):,.2f}.",
            expected=current, actual=claim.value)]
    return []


def _policy(claim: Claim, fact: Fact, ctx: VerifyContext) -> list[Violation]:
    """A policy claim must cite a policy clause, not paraphrase common sense.

    Observed live: a seller announced "all auction sales are final" on stream,
    which may conflict with platform buyer protection. Repeating an unenforceable
    disclaimer would manufacture liability for our own user.
    """
    # Three claim types share this verifier, so the check is membership
    # rather than identity — but a policy claim citing a comp fact is still
    # a mis-citation (B-33).
    if fact.kind not in (ClaimType.SHIPPING, ClaimType.RETURNS,
                         ClaimType.AUTHENTICITY):
        return [_miscite(claim, fact, "a policy claim must cite a policy fact")]
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
    ClaimType.IDENTITY: _identity, ClaimType.SIZING: _sizing,
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

    This is D-11: the only pass that catches a claim type nobody enumerated.
    Everything above it finds only what somebody anticipated.

    THE RULE: *an assertive span may go uncited only if the fact a claim
    covering this sentence cites already contains it.*

    THREE REWRITES GOT HERE, and the lesson from the two that failed is one
    sentence: **every exemption is an attack surface, and an exemption that is
    cheaper to satisfy than the assertion it guards is a hole.**

    v1 required each span to appear in some claim's QUOTE. Over-blocked at 51.9%
    — the contract asks for one claim per assertion, so a sentence carrying two
    could not have a single quote spanning both.

    v2 pooled every token in the evidence and in the buyer's question into one
    flat set. That broke in both directions at once: `$305` from a comp range
    licensed a false `$305` bid with no bid claim at all, a buyer who typed
    "is it a psa 10?" made "this is a PSA 10" assertable against a PSA 9 record,
    and the catalog's own shipping clause — quoted verbatim and cited — blocked.

    v3 (this one) narrows what may exempt, and compares properly:

      - **Only the cited fact exempts.** `claim.value` is NOT an exemption
        source: the per-type verifier is what checks the value against the
        record, and treating it as exempting let one claim carry a true figure
        and a fabricated one ("current bid 890, 12 watchers") with only the
        first checked.
      - **Numbers compare numerically** (`_numkey`). The facts hold floats and
        the notes render "$890.00"; the model writes "$890". String comparison
        blocked half the recorded corpus on the presence of cents.
      - **Words compare by an explicit lemma table** (`_LEMMA`), not a stemmer.
        The stemmer collided "lots" with "lot", and `assemble` mints a
        "lot is <status>" note for every lot — so "we have lots of these"
        exempted itself against the domain's most common noun.
      - **A question asserts nothing.** The D-14 clarifier is interrogative by
        construction and was blocking on the very attributes it exists to offer.
    KNOWN BOUND, stated rather than papered over. Exemption is per SENTENCE, not
    per phrase, so two occurrences of the same number in one sentence are
    indistinguishable: *"Orders ship within 2 business days, and we have 2 of
    these left"* passes on a cited shipping clause, because the clause's own "2"
    exempts the fabricated stock count. Closing it needs to know what each
    figure modifies, which is parsing, not matching. The per-type verifiers are
    what catch a wrong figure on a claim that cites the right fact; this pass
    only catches figures nothing is responsible for at all.

      - **The buyer's question is not consulted at all.** v2 folded it into the
        exemption set and v3's first draft kept a narrowed version; both let the
        buyer choose what the system could assert. It turned out to be
        unnecessary as well as dangerous: B-37's case — "We're at $890, so $320
        wouldn't push it" — is recognisable from the REPLY alone, because the
        reply denies the figure. `_negated_near` below does that work, and a
        denial cannot be smuggled in from outside the draft.
    """
    out: list[Violation] = []
    proper = _proper_nouns(ctx)

    # B-72. Facts and quotes do not change during a verification, so their key
    # sets are computed once each rather than once per (sentence x claim). The
    # naive version put `_keys` inside the inner loop and the tail showed it:
    # p99 12 ms and a 40 ms worst case over the recorded corpus, against a D-09
    # argument that verification is dict lookups. A claim of "0.2 ms" that is
    # only true for a two-sentence reply is not a claim about the system.
    fkeys: dict[str, set[str]] = {}

    def cited_keys(fact_id: str) -> set[str]:
        if fact_id not in fkeys:
            f = ctx.evidence.by_id(fact_id)
            fkeys[fact_id] = _fact_keys(f) if f is not None else set()
        return fkeys[fact_id]

    quoted = [(c, _norm(c.quote)) for c in draft.claims if c.quote.strip()]

    for raw in _SENTENCE.findall(draft.text):
        s = raw.strip()
        if not s or not _asserts(s):
            continue
        # B-93. This used to `continue` on any sentence ending in "?", before a
        # single span was examined — the cheapest exemption in the file, and a
        # rhetorical question asserts anything:
        #
        #   "Did you know the current bid is already $2,500?"  -> PASSED
        #   "Fancy a PSA 10 that ships free tomorrow?"         -> PASSED
        #
        # The clarifier D-14 exists to produce does need to name the attributes
        # that tell its candidates apart — but those attributes are IN the
        # ambiguity fact it cites, so they come through `cited_keys` like any
        # other backed figure. It never needed an exemption of its own.
        n = _norm(s)
        # B-106. Spans are found in the RAW sentence, not the normalised one.
        # `_norm` deletes apostrophes, which collapses "we'll" into "well" — so
        # after the B-96 fix made `_COMMITMENT` require the apostrophe (to stop
        # the ordinary word "Well," being read as a promise), the modal stopped
        # being detected at all. A fix for a false positive quietly opened a
        # false negative, which is this file's recurring failure and the reason
        # the mutation harness is in `tools/`.

        # The facts cited by claims that speak to THIS sentence. Scope is the
        # whole fix: a claim vouches for the sentence it quotes, not the draft.
        exempt: set[str] = set(proper)
        for c, qn in quoted:
            if not _overlaps(qn, n):
                continue
            # B-65. `_overlaps` is substring containment, so a quote of the
            # three characters "305" spoke for EVERY sentence containing 305 —
            # lending a comp fact's exemptions to a sentence asserting a bid,
            # and restoring a finding this docstring calls closed.
            #
            # A quote with no word in it is only unambiguous if it occurs once
            # in the whole reply. `"$24.00"` quoted against the one sentence
            # holding it is a legitimate price highlight; `"305"` against a
            # reply where 305 appears twice does not identify a sentence at all.
            # B-93. The uniqueness test used to apply only to quotes with NO
            # word in them, so a ONE-WORD quote — `quote="The"` — skipped it and
            # vouched for every sentence containing that word, handing its
            # fact's whole key set to each. Per-sentence scoping was opt-out.
            # Any short quote must now identify one sentence unambiguously.
            if (len(_norm(c.quote)) < 24
                    and _norm(draft.text).count(_norm(c.quote)) != 1):
                continue
            exempt |= cited_keys(c.source_fact_id)

        uncovered: list[str] = []
        seen: set[str] = set()
        deferred = _deferred(_soft(s))
        soft = _soft(s)
        for sp, start, end in _assertive_spans(soft):
            surface = soft[start:end]
            if sp in seen or sp in exempt or sp in deferred or surface in proper:
                continue
            # Denying a COMMITMENT is not making one: "I can't confirm Canada
            # shipping" must not need a shipping fact. Clause-scoped (B-91), so
            # a denial about something else in the same sentence cannot reach.
            #
            # A NUMBER gets no such exemption (B-92). Polarity does not make a
            # figure non-assertive — "these never sell under $1,750" is a
            # fabricated floor price wearing a denial, and it passed. The case
            # that motivated the exemption, repeating the buyer's own offer to
            # refuse it, is now a CITABLE FACT rather than a hole: `_offer`
            # mints what the buyer said, so the reply names its source like
            # anything else.
            if not sp.replace(".", "").isdigit() and _negated_at(soft, start, end):
                continue
            seen.add(sp)
            uncovered.append(sp)
        if not uncovered:
            continue
        out.append(Violation(
            code="unbacked_claim", severity=Severity.REPAIRABLE,
            message=(f"nothing backs {s!r} — nothing cites "
                     f"{', '.join(repr(u) for u in uncovered)}. Every number, "
                     f"superlative or commitment needs a claim citing a fact."),
            actual=s))
    return out


def _overlaps(quote: str, sentence: str) -> bool:
    """Does this claim's quote speak to this sentence?

    ONE direction: the quote must sit inside the sentence. The reverse — a quote
    containing the sentence — can only happen when the quote spans more than one
    sentence, which `_structural` already rejects as `quote_spans_sentences`, so
    that branch was unreachable from any draft the verifier lets through. An
    unreachable branch in a safety check is worse than no branch: nothing can
    test it, and a mutation that deletes it changes no result (B-70).

    Trailing punctuation is stripped on both sides because `_SENTENCE` keeps the
    full stop and a model quoting the whole sentence usually does not.
    """
    q, sen = quote.strip().rstrip(".!?"), sentence.strip().rstrip(".!?")
    return bool(q) and q in sen


# Whole-word number runs only. Two earlier versions got this wrong in opposite
# directions: `\d` treated "9999" as four assertions; `\d[\d,.]*` was greedy, so
# "890." never matched a quote saying "890". Word boundaries also keep the "1"
# inside "1st edition" and the "90" inside "90d" out — they are parts of words,
# not quantities, and demanding a citation for them blocked 15% of the corpus.
_NUMBER_RUN = re.compile(r"\b\d(?:[\d,.]*\d)?\b")

_WORD = re.compile(r"[a-z0-9]+")

# Numbers written as words, mapped to the value they denote so they compare
# against the record like any other figure — "two Mew items" against evidence
# holding exactly two is backed, and must not block (B-54).
# "one" is deliberately absent: here it is a pronoun ("this one") far more often
# than a quantity, and including it demanded a citation for the commonest way a
# seller refers to the item in front of them.
_NUMWORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "hundred": 100, "thousand": 1000, "dozen": 12}
_NUMWORD = re.compile(r"\b(" + "|".join(_NUMWORDS) + r")\b", re.I)

# Explicit lemmas for exactly the words `_SUPERLATIVE` and `_COMMITMENT` can
# emit (B-55). This replaced a suffix-stripping stemmer, which was wrong in both
# directions at once: it collided "lots" -> "lot" with the domain's most common
# noun, and it left "guarantee"/"guaranteed" and "deliver"/"delivery" as
# different strings, so a fact written with one form could not exempt a reply
# written with the other. A table of twenty entries is inspectable; a stemmer's
# collisions are not.
_LEMMA = {
    "ships": "ship", "shipped": "ship", "shipping": "ship", "shipment": "ship",
    "postage": "ship", "dispatch": "ship", "dispatches": "ship",
    "dispatched": "ship", "sends": "send", "sending": "send",
    "refunds": "refund", "refunded": "refund",
    "replaces": "replace", "replaced": "replace",
    "delivers": "deliver", "delivered": "deliver", "delivery": "deliver",
    "guarantees": "guarantee", "guaranteed": "guarantee",
    "promises": "promise", "promised": "promise",
    "honours": "honour", "honors": "honour", "honor": "honour",
    "covers": "cover", "covered": "cover",
    "perfectly": "perfect", "we'll": "will", "i'll": "will",
    "well": "will", "ill": "will", "shall": "will",
}


def _numkey(x: str) -> str:
    """A quantity's canonical form, compared as a NUMBER (B-56).

    The single highest-yield fix in this file. Every figure `assemble` mints is
    a float and every note renders money as `$890.00`, while the model writes
    `$890` — so a string comparison made the verdict on a true, correctly-cited
    sentence depend on whether the model typed the cents. It blocked 4 of 7
    console drafts and 3 of 8 demo drafts, and it was invisible on sold lots
    only because `_queue` happens to format those with `:,.0f`.
    """
    try:
        f = float(x.replace(",", "").rstrip("."))
    except ValueError:
        return x
    return f"{f:g}"


def _lemma(w: str) -> str:
    return _LEMMA.get(w, w)


def _keys(text: object) -> set[str]:
    """Comparable tokens: canonical numbers, and words reduced by `_LEMMA`.

    Number runs are removed from the text before the word scan so "1,320" does
    not also yield "1" and "320" — which would exempt a fabricated $320 because
    some unrelated figure contained those digits.
    """
    t = _soft(str(text))
    nums = {m.group(0) for m in _NUMBER_RUN.finditer(t)}
    keys = {_numkey(x) for x in nums}
    for x in sorted(nums, key=len, reverse=True):
        t = t.replace(x, " ")
    for w in _WORD.findall(t):
        keys.add(_lemma(w))
        if w in _NUMWORDS:
            keys.add(_numkey(str(_NUMWORDS[w])))
    return keys


# Things inside a fact that are NOT figures about the lot in hand: another
# lot's title, and any ISO timestamp. B-95 — both donated digits to the exempt
# set. `f14` on the live lot is "already sold: Armored Mewtwo SM228 PSA 10
# closed at $330", so citing it made 10 and 330 free on a RAW Charizard; and
# `ends_at='2026-09-12T...'` made 9 and 2026 free on every September auction.
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.+\-]+)?")


# The record spells conditions as trade abbreviations; a seller says them in
# full. B-96: a grade fact whose condition is "NM" could not back the words
# "Near Mint", so the correct, cited answer to the commonest condition question
# in the domain blocked on `_SUPERLATIVE` matching `mint`.
_CONDITION_WORDS = {
    "nm": "near mint", "mt": "mint", "gem": "gem mint", "lp": "light played",
    "mp": "moderately played", "hp": "heavily played", "dmg": "damaged",
    "ex": "excellent", "vg": "very good", "nmmt": "near mint mint",
}


def _fact_keys(f: Fact) -> set[str]:
    """What a fact can vouch for — its own values, not its prose furniture."""
    value = f.value
    note = f.note or ""
    if isinstance(value, dict):
        # A `title` is another lot's NAME. Its words are legitimate (that is
        # B-57), its digits are not: they belong to a different item.
        titles = " ".join(str(v) for k, v in value.items() if k == "title")
        value = {k: v for k, v in value.items() if k != "title"}
        # ...and the note usually REPEATS the title — "already sold: Armored
        # Mewtwo SM228 PSA 10 closed at $330" — so stripping the field alone
        # left the same digits reachable through prose, which is how a
        # fabricated PSA 10 on a RAW card was laundered through a sold lot's
        # name (B-95). Remove the title from the note before keying it.
        if titles:
            note = note.replace(titles, " ")
    else:
        titles = ""
    blob = _ISO.sub(" ", f"{value} {note}")
    keys = _keys(blob)
    keys |= {w for w in _WORD.findall(_norm(titles)) if not w.isdigit()}
    for abbrev, spelled in _CONDITION_WORDS.items():
        if abbrev in keys:
            keys |= set(spelled.split())
    return keys


def _proper_nouns(ctx: VerifyContext) -> set[str]:
    """Words that are NAMES in the record, not claims about the world.

    `itm_swshp_special_delivery_pikachu` is a real card in the shipped catalog,
    and `_COMMITMENT` matches "Delivery" — so "Which Pikachu do you mean, the
    Special Delivery one?" was blocked for naming a card (B-57). Names are
    exempt as WORDS only; a number in a title is still a number.
    """
    out: set[str] = set()

    def take(text: str) -> None:
        # Words only. B-68: `_WORD` is `[a-z0-9]+`, so this was silently
        # exempting every DIGIT in an identity fact or lot title too — and a
        # lot priced at $24.00 made "we have 24 left" assertable with no claim
        # at all. The docstring said "a number in a title is still a number";
        # the code did not.
        for w in _WORD.findall(_norm(text)):
            if not w.isdigit():
                out.add(_lemma(w))

    for f in ctx.evidence.facts:
        # B-95: this was `"title" in str(f.value)` — a raw SUBSTRING test, so a
        # returns clause reading "Buyers are entitled to a full refund" dumped
        # every word of the policy into the global exemption set, including
        # `refund`, `ship`, `free` and `guarantee`. A dict key, not a substring.
        if f.kind is ClaimType.IDENTITY or (
                isinstance(f.value, dict) and "title" in f.value):
            take(str(f.value))
    if ctx.lot is not None:
        take(ctx.lot.title or "")
    return out


def _deferred(sentence: str) -> set[str]:
    """A promise to DEFER is not a promise about the record (B-53).

    The verifier's own messages prescribe deferral — "defer to the host" — and
    the recorded reply following that instruction ("I'll have the host pull it
    up on camera") blocked on `I'll`, which is B-24 again: a rule refusing the
    reply its own remedy asks for.

    Only the MODAL is exempt, never the promise itself. v3's first attempt
    exempted every `_COMMITMENT` word in a deferring sentence, and because this
    is a live-*show* product where `_is_deferral` matches "show", "check" and
    "seller", *"Everything from this show ships free"* and *"I'll refund you in
    full, ask the seller"* both passed with zero claims (B-59). "ship",
    "refund", "cover" and "free" are exactly the words a policy fact has to
    back, so they are never exempt here.
    """
    if not _is_deferral(sentence):
        return set()
    return {"will"}


def _assertive_spans(sentence: str) -> list[tuple[str, int, int]]:
    """The substrings that made `_asserts` demand backing — WITH POSITIONS.

    B-90. This used to return canonical strings only, and `_negated_near` then
    searched for them in the raw sentence. Canonicalisation makes that search
    fail by construction: `_numkey("1,320")` is `"1320"` and `_lemma("we'll")`
    is `"will"`, neither of which is a substring of the text they came from. The
    search returned -1 and fell through to a fallback that read the WHOLE
    sentence for any negation — so one "no" anywhere exempted every figure:

        "I can't go lower, the current bid is $1,320 on this one."   -> PASS
        "We'll get it out to you, no worries."                      -> PASS
        "These never sell under $1,750 in this grade."               -> PASS

    Ordinary seller English, not adversarial input: any comma-grouped number and
    any contraction took that path. It also meant B-45's regression test was
    green because of this bug rather than because of `_numkey`.

    Carrying the match offsets removes the search entirely, so the window is
    always anchored on the real position and the fallback has no callers left.
    """
    out: list[tuple[str, int, int]] = []
    for m in _NUMBER_RUN.finditer(sentence):
        out.append((_numkey(m.group(0)), m.start(), m.end()))
    for m in _NUMWORD.finditer(sentence):
        out.append((_numkey(str(_NUMWORDS[m.group(0).lower()])),
                    m.start(), m.end()))
    for pat in (_SUPERLATIVE, _COMMITMENT, _UNBOUNDED):
        for m in pat.finditer(sentence):
            out.append((_lemma(m.group(0).lower()), m.start(), m.end()))
    return out


# Where one clause stops and the next begins. A negation on the far side of one
# of these is denying something else (B-91).
_CLAUSE = re.compile(r"[,;:—–]|\b(?:and|but|so|though|although|while|however)\b")


def _denies(quote: str, value: str) -> bool:
    """Does this quote deny the claimed VALUE specifically? (B-94)

    `_negated_near` searched for `_norm(value)` inside the quote and, when it
    was not found, read the whole quote for any negation. `_slug` and `_LEMMA`
    use underscore forms throughout, so a model emitting `value="1st_edition"`
    — which the catalog's own `printed` list is spelled in — took that path and
    reopened B-46 exactly:

        value="1st Edition"   "Yes, this copy is 1st Edition, no doubt"  BLOCKED
        value="1st_edition"   same reply                                 PASSED

    The flagship unrepairable case, defeated by an underscore. Locating the
    value by its content WORDS removes the spelling dependency, and a value
    whose words are not in the quote is not denied by it — `_structural`
    already requires the quote to be in the reply, so there is nothing to be
    permissive about.
    """
    q = _norm(quote)
    words = [w for w in _WORD.findall(_norm(value)) if len(w) > 1]
    if not words:
        return False
    hits = [q.find(w) for w in words if q.find(w) >= 0]
    if not hits:
        return False
    return _negated_at(q, min(hits), max(hits) + 1)


def _near_quote(reply: str, quote: str, lookahead: int = 1) -> str:
    """The sentence a claim quotes, plus the next one (B-93).

    A refusal legitimately defers in the following sentence — "that's raw, so I
    can't speak to the back. I'll have the host flip it over" — so quote scope
    alone was too tight (B-64). Whole-reply scope was too loose: it let an
    assertion be laundered by a deferral three sentences later. Two sentences is
    the span a human reads as one thought.
    """
    sentences = [x for x in _SENTENCE.findall(reply) if x.strip()]
    q = _norm(quote or "").strip()
    if not q:
        return reply
    for i, sen in enumerate(sentences):
        if q in _norm(sen) or _norm(sen).strip() in q:
            return " ".join(sentences[i:i + 1 + lookahead])
    return reply


def _clause_around(sentence: str, start: int, end: int) -> str:
    """The clause the span sits in, not the sentence.

    B-91. A positional window still reached across clause boundaries, and the
    two cases that matter look identical to a character count:

        "so $320 wouldn't push it"          the negation denies $320   -> exempt
        "Postage is on us, not something"   it denies something else   -> assert

    The difference is the comma. Windowing by distance cannot see it; windowing
    by clause can, and "denies a different thing in the same sentence" was the
    single mechanism behind three of the fatal false negatives — a fabricated
    bid of $1,320, a fabricated postage promise, and a fabricated floor price,
    all licensed by a negation that had nothing to do with them.
    """
    lo = 0
    for m in _CLAUSE.finditer(sentence, 0, start):
        lo = m.end()
    hi = len(sentence)
    m = _CLAUSE.search(sentence, end)
    if m:
        hi = m.start()
    return sentence[lo:hi]


def _negated_at(sentence: str, start: int, end: int) -> bool:
    """Is the span at [start, end) the thing this sentence denies? (B-90)

    Positional, so it cannot be defeated by a span whose canonical form differs
    from its surface form — which is what let every comma-grouped number and
    every contraction fall through to reading the whole sentence.

    Clause-scoped (B-91) rather than character-windowed, because "denies a
    different thing in the same sentence" is exactly what a fixed window cannot
    distinguish from "denies this thing".
    """
    return bool(_NEGATION.search(_clause_around(sentence, start, end)))


def _asserts(sentence: str) -> bool:
    return bool(_NUMBER.search(sentence) or _SUPERLATIVE.search(sentence)
                or _COMMITMENT.search(sentence) or _NUMWORD.search(sentence)
                or _UNBOUNDED.search(sentence))


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


def _soft(s: str) -> str:
    """`_norm`, but keeping the apostrophe (B-106).

    Coverage detects spans and compares keys, and both sides have to agree about
    what a token is. `_norm` deletes `/` — so "4/102" is one token, `4102` — and
    it deletes `'`, which collapses "we'll" into "well".

    Detecting on the raw sentence fixed the apostrophe collision and broke the
    slash agreement: raw "4/102" yields the two numbers 4 and 102, which the
    fact's normalised keys (`4102`) cannot match, so a correctly cited card
    number started blocking. Both passes use this instead, so the only
    difference from `_norm` is the one character that carries meaning here.
    """
    return re.sub(r"[^\w\s$.,'-]", "", s.lower()).strip()


def _slug(v: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_")


def _states(claim: Claim, recorded: object) -> bool:
    """Does the claim state the recorded figure ANYWHERE in its value? (B-49)

    Every numeric verifier read `_numbers(claim.value)[0]` — the FIRST number —
    so "3 bids in, currently at $890" blocked as a $3 bid while "currently at
    $890, 3 bids in" passed. Identical facts, identical truth, opposite verdicts
    decided by word order. The contract asks for one claim per assertion (B-09),
    but a claim's own value routinely carries the context around its figure.

    Presence rather than position: a claim that names the true value has named
    it. A claim that names only a false one still has no match and still blocks.
    """
    stated = _numbers(claim.value)
    if not stated or recorded is None:
        return True                       # nothing numeric to contradict
    return any(abs(x - float(recorded)) <= 0.01 for x in stated)


_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")


def _money(s: object) -> list[float]:
    """Figures written as money. See B-52: a comp's sample size and its window
    in days are numbers in the same sentence and are not prices."""
    return [float(x.replace(",", "")) for x in _MONEY.findall(str(s))]


def _numbers(s: object) -> list[float]:
    return [float(x.replace(",", "")) for x in
            re.findall(r"\d[\d,]*(?:\.\d+)?", str(s))]


def _sentence_count(s: str) -> int:
    return len([x for x in _SENTENCE.findall(s) if x.strip()])


# Widened to the contractions people actually type — "wouldn't", "can't",
# "won't" — because `_coverage` depends on recognising a refusal, and a refusal
# the pattern misses turns into a block with no repair available (B-37).
_NEGATION = re.compile(
    r"\b(not|no|never|none|cannot|can'?t|won'?t|wouldn'?t|couldn'?t|shouldn'?t|"
    r"don'?t|doesn'?t|didn'?t|isn'?t|aren'?t|wasn'?t|weren'?t|unable)\b", re.I)


def _is_negated(quote: str) -> bool:
    return bool(_NEGATION.search(quote))


def _negated_near(quote: str, value: str, window: int = 40,
                  *, after_window: int = 0) -> bool:
    """Is the CLAIMED VALUE the thing being denied? (B-46)

    Reads only the run-up to where the value appears — "this copy is not 1st
    Edition" negates it, "this copy is 1st Edition, not Shadowless" does not.
    Falls back to the whole quote when the value is not locatable in it, which
    keeps the old, permissive behaviour for claims whose value is a paraphrase
    rather than a substring; `_structural` already requires the QUOTE to be in
    the reply, so this fallback cannot be reached by inventing a quote.
    """
    q, v = quote.lower(), _norm(value).strip()
    i = q.find(v) if v else -1
    if i < 0:
        return _is_negated(quote)
    # Asymmetric, and deliberately so. English denies a noun phrase BEFORE it
    # ("not 1st Edition", "no returns") but denies a figure just AFTER it
    # ("$320 wouldn't push it"), so the trailing window has to exist — and has
    # to be short. B-67: a symmetric 48-character window let a denial about a
    # DIFFERENT thing later in the sentence exempt an earlier assertion, so
    # "All sales are final and returns are not accepted" passed with no claims,
    # which is precisely the sentence `_policy` says must never be repeated.
    return bool(_NEGATION.search(q[max(0, i - window):i + len(v) + after_window]))


def _is_deferral(quote: str) -> bool:
    return bool(re.search(
        r"\b(host|seller|check|show|camera|look|hold it up|can'?t tell|"
        r"from here|in hand)\b", quote, re.I))


def _yearless(iso: str) -> str:
    parts = iso.split("-")
    return "/".join(parts[1:]) if len(parts) == 3 else iso
