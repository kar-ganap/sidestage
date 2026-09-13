"""Suite E as unit tests — the boundaries, not the ten labelled lots.

`evals/run_moments.py` scores the observed set. These pin the decisions that
set is too small to defend on its own, each traceable to a specific lot:

  - one extension with no movement is an auction closing, not a stall (lot 6);
  - extension count measures the endgame, not demand (lot 6 vs lot 9);
  - `normal` is the default, because a nudge layer that always speaks is one
    the operator learns to ignore.
"""

from __future__ import annotations

import pytest

from app.moments import Moment, classify, nudge


def call(ext, base, now):
    return classify(extensions=ext, bid_at_first_extension=base, current_bid=now)


def test_one_extension_and_no_movement_is_not_a_stall():
    """Lot 6: 16 bids, 1 extension, $510 to $510.

    Heavy demand that simply closed. Calling this stalled would fire a nudge on
    an ordinary ending and teach the seller the signal means nothing.
    """
    assert call(1, 510, 510).moment is Moment.NORMAL


def test_two_extensions_and_no_movement_is_a_stall():
    """The timer kept resetting and nobody bid — that is the signal."""
    assert call(3, 111, 111).moment is Moment.STALLED


def test_many_extensions_with_movement_is_hot():
    assert call(24, 27, 350).moment is Moment.HOT
    assert call(7, 195, 330).moment is Moment.HOT


def test_many_extensions_without_movement_is_not_hot():
    """Contested is not the same as climbing.

    A lot can extend repeatedly on tiny increments; the nudge exists to tell the
    seller a lot is *running*, and firing it on stasis would be a false signal.
    """
    assert call(9, 200, 201).moment is not Moment.HOT


def test_a_lot_with_no_bid_history_is_normal():
    """The queued case. Nothing to measure is not the same as nothing happening."""
    assert call(0, None, None).moment is Moment.NORMAL
    assert call(3, None, 200).moment is Moment.NORMAL


def test_the_reason_is_always_readable():
    """A nudge the seller cannot account for is one they stop trusting, and this
    one fires while they are live on camera."""
    for c in (call(1, 510, 510), call(3, 111, 111), call(24, 27, 350)):
        assert c.why and not c.why.endswith(" ")


# --- the nudge itself ------------------------------------------------------


def test_normal_produces_no_nudge():
    """The common case, and the important one."""
    assert nudge(call(2, 165, 180), lot_title="Shibuya Pikachu") is None


def test_hot_nudge_is_glanceable():
    """D-35: ~5 s of reset window minus ~3 s to read and start speaking. One
    line absorbed at a glance, never a sentence."""
    n = nudge(call(7, 195, 330), lot_title="Armored Mewtwo", pop=2967)
    assert n and len(n) < 48 and "\n" not in n


def test_a_low_opening_bid_does_not_produce_a_useless_percentage():
    """Lot 8 opened at $6 and closed at $266 — '+4333%' is arithmetically right
    and unreadable in two seconds, so large moves render in dollars."""
    n = nudge(call(15, 6, 266), lot_title="Surfing Pikachu")
    assert "%" not in n and "$260" in n


@pytest.mark.parametrize("ext,base,now", [(7, 195, 330), (24, 27, 350), (3, 111, 111)])
def test_actionable_matches_the_nudge(ext, base, now):
    c = call(ext, base, now)
    assert c.actionable == (nudge(c, lot_title="x") is not None)
