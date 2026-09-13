"""D-21 — the four properties the ledger exists to provide.

Each test here corresponds to a failure that actually happens to people running
writes against a marketplace, rather than to a method on the class:

  1. a double-click must not double-apply;
  2. a response that never came back must be resolvable without guessing;
  3. the inverse must be recorded while the prior state still exists;
  4. a write that succeeds and reads back different must be *flagged*, not
     retried — because retrying a write that may have landed is how you
     double-apply.

The mock is adversarial on purpose (D-24). Faults are seeded, so a failure here
reproduces rather than being "flaky".
"""

from __future__ import annotations

import pytest

from app.actions.adapter import (
    FaultConfig,
    FloorPriceViolation,
    LotRecord,
    MockMarketplaceAdapter,
)
from app.actions.ledger import (
    CONFIRMED,
    DIVERGED,
    FAILED,
    PROPOSED,
    ROLLED_BACK,
    VERIFIED,
    Ledger,
    LedgerError,
    NotConfirmed,
)
from app.models import ActionType, LotFormat


def _lots() -> list[LotRecord]:
    return [
        LotRecord(lot_id="lot_bin", listing_id="L1", format=LotFormat.BIN,
                  status="shop", position=1, price=100.0, quantity=5,
                  floor_price=60.0, consigned=False, consignor_floor=None,
                  committed=0, version=1),
        LotRecord(lot_id="lot_con", listing_id="L2", format=LotFormat.BIN,
                  status="shop", position=2, price=200.0, quantity=2,
                  floor_price=80.0, consigned=True, consignor_floor=150.0,
                  committed=0, version=1),
        LotRecord(lot_id="lot_q1", listing_id="L3", format=LotFormat.AUCTION,
                  status="queued", position=3, price=None, quantity=1,
                  floor_price=None, consigned=False, consignor_floor=None,
                  committed=0, version=1),
        LotRecord(lot_id="lot_q2", listing_id="L4", format=LotFormat.AUCTION,
                  status="queued", position=4, price=None, quantity=1,
                  floor_price=None, consigned=False, consignor_floor=None,
                  committed=0, version=1),
    ]


def _ledger(**fault_kw) -> tuple[Ledger, MockMarketplaceAdapter]:
    ad = MockMarketplaceAdapter(_lots(), faults=FaultConfig(**fault_kw))
    return Ledger(ad), ad


# --- the state machine -----------------------------------------------------


def test_propose_sends_nothing():
    """A proposal is a question asked of the operator, not a write."""
    led, ad = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 80.0})
    assert e.status == PROPOSED
    assert ad.stats["writes_applied"] == 0
    assert ad.read_lot("lot_bin").price == 100.0


def test_execute_without_confirm_is_refused():
    """The one failure that means the product let something through.

    D-22's ladder changes *who* presses confirm. It never removes the step, so
    an unconfirmed execute is a bug in us rather than in the marketplace — which
    is why it has its own exception type.
    """
    led, _ = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 80.0})
    with pytest.raises(NotConfirmed):
        led.execute(e.id)


def test_preconditions_are_snapshotted_at_propose_time():
    """So a stale confirmation is detectable rather than silently applied."""
    led, _ = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 80.0})
    assert e.preconditions["lot_bin"]["price"] == 100.0
    assert e.preconditions["lot_bin"]["version"] == 1


def test_happy_path_reaches_verified():
    led, ad = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 80.0})
    led.confirm(e.id)
    out = led.execute(e.id)
    assert out.ok and out.entry.status == VERIFIED
    assert ad.read_lot("lot_bin").price == 80.0


# --- 1. a double-click must not double-apply -------------------------------


def test_executing_twice_does_not_apply_twice():
    """The key is minted at propose and reused, so the second call replays."""
    led, ad = _ledger()
    e = led.propose(ActionType.ADJUST_QUANTITY, {"lot_id": "lot_bin", "new_quantity": 3})
    led.confirm(e.id)
    led.execute(e.id)
    applied_once = ad.stats["writes_applied"]

    second = led.execute(e.id)          # the double-click
    assert second.ok
    assert ad.stats["writes_applied"] == applied_once, "re-applied on a repeat execute"
    assert ad.read_lot("lot_bin").quantity == 3


def test_the_idempotency_key_is_stable_across_attempts():
    """If the key were minted per attempt, retry-safety would be luck."""
    led, _ = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 90.0})
    first = e.idempotency_key
    led.confirm(e.id)
    led.execute(e.id)
    assert led.store.get(e.id).idempotency_key == first


# --- 2. a lost response must be resolvable ---------------------------------


def test_lost_response_is_retried_with_the_same_key_and_lands_once():
    """`ResponseLost` means the write may already have applied.

    Retrying blind risks a double-apply and giving up leaves the operator
    guessing. Retrying with the same key is the only correct move: the adapter's
    idempotency store answers whether it landed. The mock records the result
    *before* rolling this fault precisely so the replay is truthful.
    """
    led, ad = _ledger(lost_response_rate=1.0, seed=7)
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 70.0})
    led.confirm(e.id)
    out = led.execute(e.id)
    assert out.ok, f"never resolved: {out.message}"
    assert ad.read_lot("lot_bin").price == 70.0
    assert ad.stats["writes_applied"] == 1, "the lost response caused a second apply"


def test_a_permanent_failure_is_journalled_not_retried():
    led, ad = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 10.0})
    led.confirm(e.id)
    out = led.execute(e.id)
    assert not out.ok and out.entry.status == FAILED
    assert "FloorPriceViolation" in out.entry.error
    assert ad.read_lot("lot_bin").price == 100.0, "a refused write must change nothing"


def test_the_consignor_floor_is_not_overridable():
    """D-04's amendment: two floors with different force.

    Breaching a consignor floor is a breach of agreement, not a bad trade, so it
    must surface as its own failure rather than as an ordinary floor warning.
    """
    led, _ = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_con", "new_price": 120.0})
    led.confirm(e.id)
    out = led.execute(e.id)
    assert not out.ok
    assert "ConsignorFloor" in out.entry.error


# --- 3. the inverse is recorded at journal time ----------------------------


def test_the_inverse_carries_the_prior_value():
    """Recorded in the same breath as the write.

    Deriving it later needs the prior state, and by then someone may have moved
    the quantity twice — the information would simply be gone.
    """
    led, _ = _ledger()
    e = led.propose(ActionType.ADJUST_QUANTITY, {"lot_id": "lot_bin", "new_quantity": 1})
    led.confirm(e.id)
    led.execute(e.id)
    inv = led.store.get(e.id).inverse
    assert inv["reversible"] is True
    assert inv["params"]["new_quantity"] == 5, "inverse must restore the ORIGINAL quantity"


def test_the_prior_value_is_journalled_even_when_nothing_can_apply_it():
    """A markdown records `previous` despite being irreversible.

    "We cannot undo this, and here is what it was" is a more useful journal
    entry than silence — the operator and a reconciler both need the number even
    though no action in D-04 can write it back.
    """
    led, _ = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 75.0})
    led.confirm(e.id)
    led.execute(e.id)
    inv = led.store.get(e.id).inverse
    assert inv["reversible"] is False and inv["action"] is None
    assert inv["previous"]["price"] == 100.0


def test_a_markdown_cannot_be_taken_back(monkeypatch):
    """B-27, and the test that found it.

    The first version of this file assumed a markdown round-trips. It does not:
    `markdown` refuses any price at or above the current one, so D-04's four
    actions contain no way to raise a price, and the "inverse" I had recorded
    was unexecutable.

    The right answer is commercial rather than technical. A markdown is a public
    commitment — a buyer who saw $75 and returns to $100 was shown a price that
    was then withdrawn. Restoring it is a new decision, not a rewind.
    """
    led, ad = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 75.0})
    led.confirm(e.id)
    led.execute(e.id)
    inv = led.store.get(e.id).inverse
    assert inv["reversible"] is False
    assert inv["previous"]["price"] == 100.0, "the prior price is still journalled"
    with pytest.raises(LedgerError, match="not reversible"):
        led.compensate(e.id)


def test_compensating_a_quantity_change_restores_and_is_journalled():
    """Compensating actions, not undo: the original still happened, and the
    compensation is its own entry with its own key and its own read-back."""
    led, ad = _ledger()
    e = led.propose(ActionType.ADJUST_QUANTITY, {"lot_id": "lot_bin", "new_quantity": 2})
    led.confirm(e.id)
    led.execute(e.id)
    assert ad.read_lot("lot_bin").quantity == 2

    out = led.compensate(e.id)
    assert out.ok
    assert ad.read_lot("lot_bin").quantity == 5
    assert led.store.get(e.id).status == ROLLED_BACK
    assert len(led.journal()) == 2
    assert out.entry.id != e.id, "a compensation is a new entry with its own key"


def test_push_lot_is_not_reversible():
    """D-04b. The outgoing lot ended; re-pushing it is a new sale attempt on a
    closed lot, not a rewind. Offering an undo button here would be a lie."""
    led, _ = _ledger()
    e = led.propose(ActionType.PUSH_LOT, {"lot_id": "lot_q1"})
    led.confirm(e.id)
    led.execute(e.id)
    inv = led.store.get(e.id).inverse
    assert inv["reversible"] is False
    with pytest.raises(LedgerError, match="not reversible"):
        led.compensate(e.id)


def test_swap_is_reversible_and_round_trips():
    led, ad = _ledger()
    before = (ad.read_lot("lot_q1").position, ad.read_lot("lot_q2").position)
    e = led.propose(ActionType.SWAP_SHOWCASE, {"lot_id_a": "lot_q1", "lot_id_b": "lot_q2"})
    led.confirm(e.id)
    led.execute(e.id)
    assert (ad.read_lot("lot_q1").position, ad.read_lot("lot_q2").position) != before
    led.compensate(e.id)
    assert (ad.read_lot("lot_q1").position, ad.read_lot("lot_q2").position) == before


# --- 4. divergence is flagged, never retried -------------------------------


def test_read_back_divergence_is_flagged_and_not_retried(monkeypatch):
    """A write that succeeds and reads back different is the failure real
    marketplaces produce. Retrying it is how you double-apply, so the ledger
    stops and says so."""
    led, ad = _ledger()
    e = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 80.0})
    led.confirm(e.id)

    real_read = ad.read_lot
    def lying_read(lot_id):                      # a replica one version behind
        v = real_read(lot_id)
        return type(v)(**{**v.__dict__, "price": 100.0})
    monkeypatch.setattr(ad, "read_lot", lying_read)

    applied_before = ad.stats["writes_applied"]
    out = led.execute(e.id)
    assert not out.ok and out.entry.status == DIVERGED
    assert "price" in out.diverged
    assert out.diverged["price"] == {"expected": 80.0, "actual": 100.0}
    assert ad.stats["writes_applied"] == applied_before + 1, "divergence must not retry"


# --- durability ------------------------------------------------------------


def test_unfinished_entries_are_recoverable():
    """What a reconciler reads on restart: anything left non-terminal was
    proposed or sent but never confirmed by a read-back."""
    led, _ = _ledger()
    a = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 90.0})
    b = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 85.0})
    led.confirm(b.id)
    led.execute(b.id)
    open_ids = {x.id for x in led.store.unfinished()}
    assert a.id in open_ids and b.id not in open_ids


def test_duplicate_idempotency_keys_are_impossible():
    """Enforced by the database, not by discipline."""
    led, _ = _ledger()
    a = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 90.0})
    b = led.propose(ActionType.MARKDOWN, {"lot_id": "lot_bin", "new_price": 90.0})
    assert a.idempotency_key != b.idempotency_key
