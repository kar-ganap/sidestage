"""B-13, closed — the check the claim contract structurally cannot perform.

THE GAP. Verification asks, of each claim: *is this assertion supported by the
fact it cites?* That is answerable from evidence already in hand, which is why it
costs under a millisecond. It is also why it cannot see the failure it misses:

    Q: "is the centering good on that zard?"
    A: "It's the Base Set Charizard 4/102, shadowless print."

Every claim true. Every claim cited correctly. Nothing to object to, and the
buyer has been answered about something they did not ask. **Responsiveness is
not a property of any claim**, so no per-claim rule can reach it.

WHY IT WAS OPEN, AND HOW IT IS PAID FOR NOW. The fix is a second model call, and
the draft path is already over budget (B-08, B-14). D-38 planned to fund it with
precomputed drafts — until B-31 measured the hit rate at 4% and that plan died.

So it is funded the way streaming was (B-14): **by noticing that the operator's
reading time is dead time for the system.** The draft is shown the instant it is
verified; the judge runs *while they read it*. Measured at **p50 2.0 s, p95
2.1 s** — so on the path that matters, question asked to message sent, it is
usually free. Not always: an operator who sends within two seconds waits out the
remainder, which `send()` does deliberately rather than skipping the check.

It is not faster than running inline. It is scheduled against time that was
already being spent, which is the same trick streaming played with generation.

WHAT IT DOES NOT DO. It is advisory. A judge objection surfaces as a **warning
beside the send button**, not a block. Two reasons, and the second is the
stronger. The verifier blocks because it can point at a fact and say "this
contradicts the record" — a judge can only say "this reads as unresponsive",
which is a judgement rather than a finding. And B-24 is recent: a rule that
blocks on its own opinion, on a path no adversarial suite can audit, is how you
build a system that refuses good work and calls it safety.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger("sidestage.judge")

# Sonnet, not Opus, and measured rather than assumed (B-32).
#
# D-17 says the eval grader should be stronger than what it judges, and that is
# right — there is no latency budget on an offline grader. This judge has the
# same ROLE in a different POSITION, and the calculus flips: on 12 cases both
# models scored 12/12, and Sonnet's tail is three times tighter (p95 2.1s vs
# 6.1s). On a path racing an operator's attention the tail is the number.
#
# Same distinction as B-20: identical question, opposite right answer depending
# on where it runs. SIDESTAGE_JUDGE_MODEL overrides so the comparison stays
# runnable rather than becoming a claim in a comment.
JUDGE_MODEL = os.getenv("SIDESTAGE_JUDGE_MODEL", "claude-sonnet-5")

SYSTEM = """\
You are the last check on a reply a live trading-card seller is about to post.

Every factual claim in it has ALREADY been verified against the listing. Do not \
re-check facts; assume they are correct. You are looking for the one thing that \
check cannot see:

    DOES THIS REPLY ANSWER THE QUESTION THAT WAS ACTUALLY ASKED?

Answer "unresponsive" only when a buyer reading it would still not know the \
answer to their question — because it addresses a different attribute, answers \
about a different item, or states true things that are beside the point.

These are RESPONSIVE and must not be flagged:
  - a direct answer, however short
  - a correct refusal: "that's raw, so I can't say for sure on the back"
  - declining with a reason: "not enough recent sales to quote a comp"
  - asking which item they mean, when the reference was ambiguous
  - answering the question and adding one relevant fact

Sellers type fast and informally. Terseness is not unresponsiveness.
"""


@dataclass(frozen=True)
class Opinion:
    responsive: bool
    why: str
    latency_ms: int
    model: str = JUDGE_MODEL
    errored: bool = False

    @property
    def warns(self) -> bool:
        return not self.responsive and not self.errored


def _fallback(msg: str, ms: int) -> Opinion:
    """A judge that cannot run must not become a blocker.

    Advisory means advisory: if the second opinion is unavailable, the operator
    gets the draft and no warning, exactly as they did before this existed.
    """
    return Opinion(responsive=True, why=msg, latency_ms=ms, errored=True)


def judge(question: str, reply: str) -> Opinion:
    """One call. Blocking — see `JudgeRunner` for the concurrent path."""
    t0 = time.perf_counter()
    if not reply.strip():
        return Opinion(True, "empty reply — nothing to assess", 0)
    if not settings.anthropic_api_key:
        return _fallback("no credential; judge skipped", 0)
    try:
        import anthropic
        from pydantic import BaseModel, Field

        class Out(BaseModel):
            responsive: bool
            why: str = Field(max_length=180, description="one short sentence")

        r = anthropic.Anthropic().messages.parse(
            model=JUDGE_MODEL, max_tokens=1024,
            # The system prompt is stable and cached; only the pair varies.
            system=[{"type": "text", "text": SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": f"QUESTION: {question}\n\nREPLY: {reply}"}],
            output_format=Out,
            thinking={"type": "disabled"},   # a yes/no with a reason (B-20)
        )
        ms = int((time.perf_counter() - t0) * 1000)
        o = r.parsed_output
        if o is None:                         # B-12/B-17
            return _fallback("judge returned nothing parsable", ms)
        return Opinion(o.responsive, o.why, ms)
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        log.warning("judge unavailable: %s", type(exc).__name__)
        return _fallback(f"judge unavailable ({type(exc).__name__})", ms)


class JudgeRunner:
    """Runs the judge against the operator's reading time rather than before it.

    The draft is already on screen when this starts. `result()` is what the send
    action consults; if the operator is quicker than the judge it waits out the
    remainder, which is the only case where this costs anything at all.
    """

    def __init__(self, workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="judge")
        self._pending: dict[str, Future] = {}
        self._lock = threading.Lock()

    def start(self, card_id: str, question: str, reply: str) -> None:
        with self._lock:
            self._pending[card_id] = self._pool.submit(judge, question, reply)

    def result(self, card_id: str, *, timeout: float = 2.5) -> Opinion | None:
        with self._lock:
            fut = self._pending.get(card_id)
        if fut is None:
            return None
        try:
            return fut.result(timeout=timeout)
        except Exception:
            # Still running, or it failed. Either way the operator is not held up.
            return None

    def ready(self, card_id: str) -> bool:
        with self._lock:
            fut = self._pending.get(card_id)
        return bool(fut and fut.done())

    def forget(self, card_id: str) -> None:
        with self._lock:
            self._pending.pop(card_id, None)
