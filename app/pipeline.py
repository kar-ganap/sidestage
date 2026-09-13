"""The core loop. If you read one file in this repo, read this one.

    message
      -> 1  resolve    what is this about?                 entities.resolve
      -> 2  assemble   fetch everything assertable          evidence.assemble
      -> 3  draft      model writes reply + claims          llm.draft
      -> 4  verify     check each claim against its fact    verify.verify
      -> 5  repair     one bounded retry, if it could help  (this file)
      -> 6  settle     pass / repaired / blocked            (this file)

Step 5 is the only part with judgement in it, and the judgement is small: retry
only when *every* violation is repairable. One unrepairable violation poisons the
batch, because no rewording makes a false claim true and a retry costs ~2.3 s we
do not have (D-10b).

Step 6 never throws. A blocked draft is a product surface, not an error — the
operator sees the refused text and the reason beside it, which is what earns the
permission to automate later (D-23).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.catalog import Catalog, get_catalog
from app.entities import EntityResolver, Resolution, get_resolver
from app.evidence import assemble
from app.llm import DraftOutput, LLMClient, get_client
from app.models import (
    Claim,
    ClaimType,
    Draft,
    Evidence,
    Intent,
    Lot,
    Severity,
    Verdict,
    Violation,
)
from app.verify import VerifyResult, verify

# What goes out when everything else failed. Deliberately contains no number, no
# superlative and no commitment, so it passes the verifier on its merits rather
# than by exemption — a fallback that needed an exemption would be a hole.
SAFE_FALLBACK = "Let me check that one and come right back to you."


@dataclass
class PipelineResult:
    """Everything the console renders and the ledger stores."""

    draft: Draft
    evidence: Evidence
    resolution: Resolution
    verification: VerifyResult
    attempts: int
    latency_ms: dict[str, int]
    first_text: str = ""
    """What the model wrote on attempt 0, before verification saw it.

    Kept so Spike 1's ablation can compare verified against unverified on the
    SAME generation. Running the two arms as two separate calls compares two
    independent samples of a stochastic model, and at n=89 the sampling noise
    was larger than the effect being measured — the first run showed the
    verifier catching 4 cases and "missing" 4, where the 4 misses were simply a
    different roll of the same dice.
    """
    ttft_ms: int = 0
    """First readable token of the first attempt.

    Held outside `latency_ms` deliberately: that dict is summed for `total_ms`,
    and TTFT is a prefix of the draft step rather than a step of its own, so
    putting it there would count the same milliseconds twice.
    """

    @property
    def sendable(self) -> bool:
        return self.draft.verdict in (Verdict.PASS, Verdict.REPAIRED)

    @property
    def total_ms(self) -> int:
        """When the operator can *send* — verification gates the send, so this
        includes every claim arriving. Compare against `ttft_ms`, which is when
        they can start *reading*; the gap between the two is time the operator
        spends reading rather than waiting."""
        return sum(self.latency_ms.values())


def draft_reply(
    message: str,
    *,
    intent: Intent,
    lot: Lot | None = None,
    catalog: Catalog | None = None,
    resolver: EntityResolver | None = None,
    client: LLMClient | None = None,
    max_repairs: int = 1,
    on_text: Callable[[str], None] | None = None,
) -> PipelineResult:
    cat = catalog or get_catalog()
    res_ = resolver or get_resolver()
    llm = client or get_client()
    timing: dict[str, int] = {}

    # 1 — what is this about?
    t = _tick()
    resolution = res_.resolve(message)
    timing["resolve"] = _tock(t)

    # 2 — everything assertable, fetched before a word is generated (D-09)
    t = _tick()
    ev = assemble(intent=intent, resolution=resolution, catalog=cat, lot=lot)
    timing["assemble"] = _tock(t)

    attempt, feedback, result, ttft = 0, None, None, 0
    first_text = ""
    while True:
        # 3 — draft. `on_text` is passed straight through, so a console can
        # render the reply as it arrives while the claims are still decoding.
        t = _tick()
        out = llm.draft(question=message, evidence=ev, intent=intent,
                        repair=feedback, on_text=on_text)
        timing[f"draft_{attempt}"] = _tock(t)
        # First attempt only. A repair's first token lands after the operator has
        # already read a draft we then threw away, so it is not "time to read".
        if not attempt:
            ttft = out.ttft_ms

        draft = _to_draft(out.output, ev, out.model, attempt)
        if not attempt:
            first_text = draft.text

        # 4 — verify, against the fact each claim cited
        t = _tick()
        result = verify(draft, ev, catalog=cat)
        timing[f"verify_{attempt}"] = _tock(t)

        # An empty reply is not a pass. Nothing to verify is not the same as
        # nothing wrong, and the verifier's passes are all "is this assertion
        # supported" — none of them fire on the absence of assertions.
        if result.verdict is Verdict.PASS and draft.text.strip():
            draft.verdict = Verdict.REPAIRED if attempt else Verdict.PASS
            draft.violations = []
            break
        draft.violations = list(result.violations)
        if not draft.text.strip():
            draft.violations.append(Violation(
                code="empty_generation", severity=Severity.REPAIRABLE,
                message="the model returned nothing usable (truncated or refused)"))

        # 5 — repair, only when a rewrite could actually help
        if attempt < max_repairs and result.repairable:
            feedback = result.feedback()
            attempt += 1
            continue

        # 6 — blocked. The operator sees the refused text AND the reason (D-23),
        # plus something safe they can send instead without retyping.
        draft.verdict = Verdict.BLOCKED
        draft.fallback_text = SAFE_FALLBACK
        break

    draft.latency_ms = dict(timing)
    return PipelineResult(draft=draft, evidence=ev, resolution=resolution,
                          verification=result, attempts=attempt + 1,
                          latency_ms=timing, first_text=first_text, ttft_ms=ttft)


def _to_draft(out: DraftOutput | None, ev: Evidence, model: str, attempt: int) -> Draft:
    """Model output -> our own type.

    An unknown claim type is dropped rather than crashing the turn, and dropping
    it is safe *because* of the coverage backstop: the sentence it was meant to
    cover becomes unbacked, so D-11 blocks it. A pass that only works when the
    model behaves would not be a guarantee.

    BUILD-LOG B-12. `parsed_output` is None when generation is truncated or
    refused — found by running 154 eval cases rather than by unit tests, because
    it needs a real model under real variation to occur at all. An empty draft is
    the right representation: it asserts nothing, so it blocks on its own merits
    rather than needing a special case.
    """
    if out is None:
        return Draft(id=f"d_{uuid.uuid4().hex[:8]}", card_id="", text="",
                     claims=[], evidence_id=ev.id, model=model, attempt=attempt,
                     created_at=datetime.now(UTC))
    claims: list[Claim] = []
    for c in out.claims:
        try:
            kind = ClaimType(c.type)
        except ValueError:
            continue
        claims.append(Claim(type=kind, value=str(c.value),
                            source_fact_id=c.source_fact_id, quote=c.quote))
    return Draft(
        id=f"d_{uuid.uuid4().hex[:8]}", card_id="", text=out.reply_text.strip(),
        claims=claims, evidence_id=ev.id, model=model, attempt=attempt,
        created_at=datetime.now(UTC))


def _tick() -> float:
    from time import perf_counter
    return perf_counter()


def _tock(t0: float) -> int:
    from time import perf_counter
    return int((perf_counter() - t0) * 1000)
