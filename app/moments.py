"""D-26b — is this lot hot, stalled, or ordinary?

**Deliberately not a model.** Two numbers from the auction — how many times the
timer extended, and how far the price moved during those extensions — decide it.
The suite that scores this (`evals/run_moments.py`) therefore tests *threshold
placement*, not a classifier, and every decision can be recomputed by hand from
the observation table.

WHY EXTENSION COUNT AND NOT BID COUNT. This is the finding that makes the rule
work, and it came from the field rather than from design. Lot 9 took 22 bids
against 11 extensions — **half the bids landed after the timer would otherwise
have expired**. Lot 6 took 16 bids against 1 extension: similar demand, 6% late.
Extensions fire only on bids arriving inside the reset threshold, so the count
isolates *the contested endgame* rather than total interest. A lot with heavy
early bidding and a quiet close is not a moment; a lot with a bidding war in the
last ten seconds is, and only extension count tells them apart.

WHAT IT IS FOR. `hot` and `stalled` are the triggers for the whole nudge layer
(D-05) — the one product surface that has to act *during* a lot rather than
after it. And the window shrinks as a lot gets more valuable: timers always
reset to less than the base timer, so extension 3 carries roughly three times
the runway of extension 15. Detecting late is the same as not detecting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# --- thresholds, and what each is doing --------------------------------------
#
# Placed from the observed distribution (1, 2, 3, 3, 6, 7, 7, 11, 15+, 24),
# which is strongly bimodal: a cluster at 1-3 and a cluster at 6+, with nothing
# between 3 and 6. The gap is where the line goes, and it was not chosen to make
# the labels come out right — it is the only place a line can sit without
# splitting a cluster.
HOT_EXTENSIONS = 5          # sits inside the empty 3..6 gap
HOT_DELTA_PCT = 0.15        # the price actually moved during the endgame

# A stall needs the timer to have extended at least twice — once is an auction
# simply closing, which is the ordinary case and not a signal.
STALL_EXTENSIONS = 2
STALL_DELTA_ABS = 0.0       # exactly zero: a stall is bids that stopped arriving


class Moment(StrEnum):
    HOT = "hot"
    STALLED = "stalled"
    NORMAL = "normal"


@dataclass(frozen=True)
class MomentCall:
    """The classification plus the arithmetic that produced it.

    `why` exists for the same reason the triage scorer reports its features: a
    nudge the seller cannot account for is a nudge they stop trusting, and this
    one fires while they are live on camera.
    """

    moment: Moment
    extensions: int
    delta: float
    delta_pct: float
    why: str

    @property
    def actionable(self) -> bool:
        return self.moment is not Moment.NORMAL


def classify(
    *,
    extensions: int,
    bid_at_first_extension: float | None,
    current_bid: float | None,
) -> MomentCall:
    """Two numbers in, one call out.

    `bid_at_first_extension` rather than the starting bid is deliberate: the
    question is what happened *during the contested close*, and measuring from
    the opening price would score a lot that ran up early and then died as hot.
    Lot 4 opened at $27 and reached $350, but the number that matters is that
    92% of that movement happened across 24 extensions.
    """
    base = bid_at_first_extension
    now = current_bid
    if base is None or now is None:
        return MomentCall(Moment.NORMAL, extensions, 0.0, 0.0,
                          "no bid history yet — nothing to measure")

    delta = float(now) - float(base)
    pct = (delta / base) if base else 0.0

    if extensions >= HOT_EXTENSIONS and pct >= HOT_DELTA_PCT:
        return MomentCall(
            Moment.HOT, extensions, delta, pct,
            f"{extensions} extensions and the price moved "
            f"${delta:,.0f} ({pct:.0%}) since the first one")

    if extensions >= STALL_EXTENSIONS and delta <= STALL_DELTA_ABS:
        return MomentCall(
            Moment.STALLED, extensions, delta, pct,
            f"the timer has extended {extensions} times and the bid has not "
            f"moved from ${base:,.0f}")

    if extensions >= HOT_EXTENSIONS:
        return MomentCall(
            Moment.NORMAL, extensions, delta, pct,
            f"{extensions} extensions but only {pct:.0%} movement — "
            f"contested, not climbing")

    return MomentCall(
        Moment.NORMAL, extensions, delta, pct,
        f"{extensions} extension{'s' if extensions != 1 else ''}, "
        f"${delta:,.0f} movement — an ordinary close")


# --- the nudge ---------------------------------------------------------------
#
# One line, spoken aloud, absorbed at a glance. The auction mechanic dictates
# the format: the seller has roughly 5 s of reset window minus ~3 s to read and
# start speaking, so a sentence is already too long (D-35).

def nudge(call: MomentCall, *, lot_title: str, pop: int | None = None,
          comp_high: float | None = None) -> str | None:
    """What to put on screen, or None when there is nothing worth saying.

    Returning None is the common case and the important one. A nudge layer that
    always has something to say trains the operator to ignore it.
    """
    if call.moment is Moment.HOT:
        # Absolute movement above a certain point; a percentage is meaningless
        # off a low opening bid. Lot 8 opened at $6 and closed at $266, which is
        # "+4333%" — arithmetically right and useless to read aloud in a
        # two-second glance (D-35: the nudge is glanceable, not readable).
        move = (f"+{call.delta_pct:.0%}" if call.delta_pct < 3
                else f"+${call.delta:,.0f}")
        bits = [f"{call.extensions} ext · {move}"]
        if pop is not None:
            bits.append(f"pop {pop:,}")
        if comp_high is not None and call.delta:
            bits.append(f"comps to ${comp_high:,.0f}")
        return " · ".join(bits)
    if call.moment is Moment.STALLED:
        return f"stalled at ${call.delta + 0:,.0f} move · {call.extensions} ext"
    return None
