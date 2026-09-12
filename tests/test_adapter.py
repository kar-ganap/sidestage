"""Tests for the marketplace seam (D-24).

Every test is deterministic: the fault injector is seeded, and injected latency
advances a virtual clock rather than sleeping, so a 900 ms tail and a 500 ms
consistency window cost microseconds. Nothing here is probabilistic — a fault
that fires, fires because the test asked for it.

The load-bearing cases are the two where the caller's belief and the
marketplace's state disagree: partial application (the write landed, the
response did not) and eventual consistency (the write landed, the read-back
cannot see it yet). Both are places where an honest-looking retry double-applies
if the adapter is not idempotent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.actions.adapter import (
    AdapterError,
    ConsignorFloorViolation,
    FaultConfig,
    FloorPriceViolation,
    FormatNotWritable,
    IdempotencyKeyReuse,
    InvalidPrice,
    InvalidQuantity,
    LotNotFound,
    LotRecord,
    LotStateConflict,
    MarketplaceAdapter,
    MockMarketplaceAdapter,
    OversellRisk,
    RateLimited,
    ResponseLost,
    UpstreamUnavailable,
)
from app.models import ActionType, Lot, LotFormat


def _records() -> list[LotRecord]:
    """A slice of `data/catalog.json`, plus one consigned lot.

    Catalog rows go through `LotRecord.from_lot` so the real `Lot` type stays
    exercised; the consigned row goes through `from_dict`, because `Lot` has no
    `consigned` / `consignor_floor` fields — D-04's amendment postdates it.
    """
    catalog = [
        Lot(
            id="lot_s01", listing_id="L-95001", item_id="itm_frlg_gengar",
            title="Gengar FireRed & LeafGreen PSA 9", format=LotFormat.BIN,
            status="shop", price=185.0, quantity=1, floor_price=150.0, cost_basis=96.0,
        ),
        Lot(
            id="lot_s02", listing_id="L-95002", item_id="itm_cel_mew",
            title="Mew Celebrations 011/025 raw NM", format=LotFormat.BIN,
            status="shop", price=24.0, quantity=3, floor_price=18.0, cost_basis=11.0,
        ),
        Lot(
            id="lot_006", listing_id="L-90006", item_id="itm_base_charizard_raw",
            title="Base Set Charizard SHADOWLESS raw NM", format=LotFormat.AUCTION,
            status="live", position=6, starting_bid=250.0, current_bid=890.0,
        ),
        Lot(
            id="lot_007", listing_id="L-90007", item_id="itm_cp_charizard_vmax",
            title="Charizard VMAX Champion's Path PSA 10", format=LotFormat.AUCTION,
            status="queued", position=7, starting_bid=400.0,
        ),
        Lot(
            id="lot_008", listing_id="L-90008", item_id="itm_frlg_blaziken_ex",
            title="Blaziken ex FireRed & LeafGreen PSA 8", format=LotFormat.AUCTION,
            status="queued", position=8, starting_bid=60.0,
        ),
        Lot(
            id="lot_009", listing_id="L-90009", item_id="itm_neo_shining_dragonite",
            title="Shining Dragonite Neo Destiny raw LP", format=LotFormat.AUCTION,
            status="queued", position=9, starting_bid=120.0,
        ),
    ]
    records = [LotRecord.from_lot(lot) for lot in catalog]
    # One unit of the Mew is already promised to a buyer — the oversell guard.
    records[1] = LotRecord.from_lot(catalog[1], committed=1)
    records.append(
        LotRecord.from_dict(
            {
                "id": "lot_s06", "listing_id": "L-95006", "format": "bin",
                "status": "shop", "price": 240.0, "quantity": 1,
                "floor_price": 150.0,
                # The seller does not own this card, and 200 is not theirs to move.
                "consigned": True, "consignor_floor": 200.0,
            }
        )
    )
    return records


def _adapter(**faults: object) -> MockMarketplaceAdapter:
    """A marketplace with every fault off unless the test asks for one.

    Zero jitter and no tail by default, so latency is exactly `latency_ms` per
    call and the consistency arithmetic in these tests is readable.
    """
    config = FaultConfig(  # type: ignore[arg-type]
        seed=1729, latency_ms=20, latency_jitter_ms=0, **faults
    )
    return MockMarketplaceAdapter(_records(), faults=config)


# ---------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------


def test_mock_satisfies_the_protocol_and_covers_the_four_write_actions() -> None:
    adapter = _adapter()
    assert isinstance(adapter, MarketplaceAdapter)
    seen = {
        adapter.push_lot("lot_008", idempotency_key="k1").action,
        adapter.swap_showcase("lot_007", "lot_009", idempotency_key="k2").action,
        adapter.markdown("lot_s01", 170.0, idempotency_key="k3").action,
        adapter.adjust_quantity("lot_s02", 2, idempotency_key="k4").action,
    }
    assert seen == {
        ActionType.PUSH_LOT,
        ActionType.SWAP_SHOWCASE,
        ActionType.MARKDOWN,
        ActionType.ADJUST_QUANTITY,
    }


def test_a_write_reports_the_state_it_replaced() -> None:
    # D-21 journals the inverse op at journal time, so the result has to carry
    # the pre-write value; deriving it later needs state we may no longer have.
    adapter = _adapter()
    result = adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    assert result.previous == {"price": 185.0}
    assert result.applied == {"price": 170.0}
    assert result.versions == {"lot_s01": 2}


def test_unknown_lot_is_not_found() -> None:
    adapter = _adapter()
    with pytest.raises(LotNotFound):
        adapter.markdown("lot_nope", 10.0, idempotency_key="k")


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


def test_replay_returns_the_original_result_and_does_not_double_apply() -> None:
    adapter = _adapter()
    first = adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    second = adapter.markdown("lot_s01", 170.0, idempotency_key="k")

    assert not first.replayed and second.replayed
    assert second.applied == first.applied
    assert second.previous == first.previous
    # The version is the proof. A second apply would bump it again, and a second
    # markdown off the new base would land at 155, not 170.
    assert second.versions == first.versions == {"lot_s01": 2}
    assert adapter.read_lot("lot_s01").price == 170.0
    assert adapter.stats["writes_applied"] == 1


def test_replay_of_a_refusal_is_the_same_refusal() -> None:
    # Same key must mean same outcome, or "replay is safe" is a claim rather
    # than a property.
    adapter = _adapter()
    with pytest.raises(FloorPriceViolation):
        adapter.markdown("lot_s01", 140.0, idempotency_key="k")
    with pytest.raises(FloorPriceViolation):
        adapter.markdown("lot_s01", 140.0, idempotency_key="k")
    assert adapter.read_lot("lot_s01").price == 185.0


def test_one_key_cannot_carry_two_different_requests() -> None:
    adapter = _adapter()
    adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    with pytest.raises(IdempotencyKeyReuse):
        adapter.markdown("lot_s01", 160.0, idempotency_key="k")
    assert adapter.read_lot("lot_s01").price == 170.0


# ---------------------------------------------------------------------
# Partial application — the case idempotency exists for
# ---------------------------------------------------------------------


def test_lost_response_applies_the_write_the_caller_believes_failed() -> None:
    adapter = _adapter(lost_response_rate=1.0)
    with pytest.raises(ResponseLost) as raised:
        adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    assert raised.value.idempotency_key == "k"

    # The caller saw a failure. The marketplace applied it anyway — and this is
    # how the reconciler finds out: the version moved.
    view = adapter.read_lot("lot_s01")
    assert view.price == 170.0
    assert view.version == 2


def test_retry_after_a_lost_response_leaves_exactly_one_effect() -> None:
    adapter = _adapter(lost_response_rate=1.0)
    with pytest.raises(ResponseLost):
        adapter.markdown("lot_s01", 170.0, idempotency_key="k")

    retry = adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    assert retry.replayed
    assert retry.applied == {"price": 170.0}
    assert retry.previous == {"price": 185.0}   # still the pre-write value, not 170

    view = adapter.read_lot("lot_s01")
    assert view.price == 170.0
    assert view.version == 2                    # one bump, not two
    assert adapter.stats["writes_applied"] == 1


# ---------------------------------------------------------------------
# Eventual consistency and stale reads
# ---------------------------------------------------------------------


def test_read_back_lags_a_successful_write() -> None:
    adapter = _adapter(read_lag_ms=500)
    result = adapter.markdown("lot_s01", 170.0, idempotency_key="k")

    view = adapter.read_lot("lot_s01")
    assert view.price == 185.0                       # the OLD value
    assert view.version < result.versions["lot_s01"]
    # The write did not fail. A caller that treats this as failure and re-issues
    # under a fresh key is how you get a double markdown.


def test_the_caller_must_retry_the_read_rather_than_assume_failure() -> None:
    adapter = _adapter(read_lag_ms=500)
    result = adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    target = result.versions["lot_s01"]

    reads = 0
    view = adapter.read_lot("lot_s01")
    reads += 1
    while view.version < target and reads < 50:
        view = adapter.read_lot("lot_s01")
        reads += 1

    assert reads > 1, "the lag should have cost at least one extra read"
    assert view.version == target
    assert view.price == 170.0


def test_a_read_can_be_served_one_version_behind() -> None:
    adapter = _adapter(stale_read_rate=1.0)
    adapter.markdown("lot_s01", 170.0, idempotency_key="k")

    stale = adapter.read_lot("lot_s01")
    assert stale.price == 185.0 and stale.version == 1
    assert adapter.stats["stale_reads"] == 1

    adapter.faults = replace(adapter.faults, stale_read_rate=0.0)
    assert adapter.read_lot("lot_s01").price == 170.0


# ---------------------------------------------------------------------
# Rate limiting and transient failure
# ---------------------------------------------------------------------


def test_rate_limiting_surfaces_a_usable_retry_after() -> None:
    adapter = _adapter(rate_limit_per_window=1, rate_limit_window_ms=1_000)
    adapter.markdown("lot_s01", 170.0, idempotency_key="k1")     # t=20, allowed

    with pytest.raises(RateLimited) as raised:
        adapter.markdown("lot_s02", 20.0, idempotency_key="k2")  # t=40, 429
    assert raised.value.retry_after_ms == 960                    # 1000 - 40, derived

    # It is a real number: waiting exactly that long clears the limit, and the
    # key that was rejected is still usable.
    adapter.clock.sleep(raised.value.retry_after_ms)
    result = adapter.markdown("lot_s02", 20.0, idempotency_key="k2")
    assert result.applied == {"price": 20.0}
    assert not result.replayed


def test_a_rate_limited_write_never_reached_the_lot() -> None:
    adapter = _adapter(rate_limit_per_window=1, rate_limit_window_ms=1_000)
    adapter.markdown("lot_s01", 170.0, idempotency_key="k1")
    with pytest.raises(RateLimited):
        adapter.markdown("lot_s02", 20.0, idempotency_key="k2")

    adapter.clock.advance(1_000)                                 # fresh window
    assert adapter.read_lot("lot_s02").price == 24.0


def test_a_transient_error_does_not_burn_the_idempotency_key() -> None:
    # The request never completed, so the key must still be spendable —
    # otherwise a 503 would permanently strand the action.
    adapter = _adapter(transient_error_rate=1.0)
    with pytest.raises(UpstreamUnavailable):
        adapter.markdown("lot_s01", 170.0, idempotency_key="k")

    adapter.faults = replace(adapter.faults, transient_error_rate=0.0)
    result = adapter.markdown("lot_s01", 170.0, idempotency_key="k")
    assert not result.replayed
    assert result.applied == {"price": 170.0}


# ---------------------------------------------------------------------
# Domain guards: the two floors (D-04) and the format gate (D-03)
# ---------------------------------------------------------------------


def test_markdown_below_floor_price_is_refused() -> None:
    adapter = _adapter()
    with pytest.raises(FloorPriceViolation) as raised:
        adapter.markdown("lot_s01", 140.0, idempotency_key="k")

    assert raised.value.floor == 150.0
    assert raised.value.overridable is True          # the seller's own margin call
    assert not isinstance(raised.value, ConsignorFloorViolation)
    assert adapter.read_lot("lot_s01").price == 185.0


def test_markdown_below_consignor_floor_is_a_distinct_error() -> None:
    # Breaching a consignor floor is a breach of agreement, not a bad trade
    # (D-04, amended). The caller must be able to tell them apart by type, and
    # `except FloorPriceViolation` must not catch this one.
    adapter = _adapter()
    with pytest.raises(ConsignorFloorViolation) as raised:
        adapter.markdown("lot_s06", 190.0, idempotency_key="k")

    assert raised.value.consignor_floor == 200.0
    assert raised.value.overridable is False
    assert not isinstance(raised.value, FloorPriceViolation)
    assert adapter.read_lot("lot_s06").price == 240.0


def test_a_price_breaching_both_floors_reports_the_absolute_one() -> None:
    # lot_s06 has floor_price 150 and consignor_floor 200. At 120 both are
    # breached; reporting the overridable floor would offer the operator an
    # override they must never be given.
    adapter = _adapter()
    with pytest.raises(ConsignorFloorViolation):
        adapter.markdown("lot_s06", 120.0, idempotency_key="k")


def test_markdown_on_an_auction_lot_is_refused_entirely() -> None:
    # D-03: you cannot lower a bid. Not a floor question — the action is not
    # legal on this format at all.
    adapter = _adapter()
    with pytest.raises(FormatNotWritable) as raised:
        adapter.markdown("lot_006", 500.0, idempotency_key="k")

    assert raised.value.format is LotFormat.AUCTION
    assert raised.value.action is ActionType.MARKDOWN
    assert not isinstance(raised.value, (FloorPriceViolation, ConsignorFloorViolation))


def test_a_markdown_that_raises_the_price_is_refused() -> None:
    adapter = _adapter()
    with pytest.raises(InvalidPrice):
        adapter.markdown("lot_s01", 200.0, idempotency_key="k")


# ---------------------------------------------------------------------
# Domain guards: quantity
# ---------------------------------------------------------------------


def test_adjust_quantity_below_zero_is_refused() -> None:
    adapter = _adapter()
    with pytest.raises(InvalidQuantity) as raised:
        adapter.adjust_quantity("lot_s02", -1, idempotency_key="k")

    assert raised.value.requested == -1
    assert adapter.read_lot("lot_s02").quantity == 3


def test_adjust_quantity_below_committed_stock_is_refused() -> None:
    # D-04's oversell guard: one unit is already promised to a buyer.
    adapter = _adapter()
    with pytest.raises(OversellRisk):
        adapter.adjust_quantity("lot_s02", 0, idempotency_key="k")
    assert adapter.adjust_quantity("lot_s02", 1, idempotency_key="k2").applied == {
        "quantity": 1
    }


def test_adjust_quantity_on_an_auction_lot_is_refused() -> None:
    adapter = _adapter()
    with pytest.raises(FormatNotWritable):
        adapter.adjust_quantity("lot_006", 2, idempotency_key="k")


# ---------------------------------------------------------------------
# Showcase writes — legal on auctions, because they do not touch bid state
# ---------------------------------------------------------------------


def test_push_lot_demotes_the_live_lot_reversibly() -> None:
    adapter = _adapter()
    result = adapter.push_lot("lot_008", idempotency_key="k")

    assert result.applied["live_lot_id"] == "lot_008"
    assert result.previous["live_lot_id"] == "lot_006"   # the ledger's inverse op
    assert adapter.read_lot("lot_008").status == "live"
    # Demoted, not ended. Ending it would make the push consequential and cap
    # the action at L1 on the ladder (D-22).
    assert adapter.read_lot("lot_006").status == "queued"


def test_push_lot_refuses_a_lot_that_already_sold() -> None:
    records = [
        replace(rec, status="sold") if rec.lot_id == "lot_008" else rec
        for rec in _records()
    ]
    adapter = MockMarketplaceAdapter(records, faults=FaultConfig(seed=1729))
    with pytest.raises(LotStateConflict):
        adapter.push_lot("lot_008", idempotency_key="k")


def test_swap_showcase_exchanges_positions_and_ignores_argument_order() -> None:
    adapter = _adapter()
    result = adapter.swap_showcase("lot_008", "lot_009", idempotency_key="k")

    assert result.previous["positions"] == {"lot_008": 8, "lot_009": 9}
    assert adapter.read_lot("lot_008").position == 9
    assert adapter.read_lot("lot_009").position == 8

    # The mirrored call is the same request, so the same key replays rather than
    # swapping back — a double-applied swap would silently undo itself.
    mirrored = adapter.swap_showcase("lot_009", "lot_008", idempotency_key="k")
    assert mirrored.replayed
    assert adapter.read_lot("lot_008").position == 9


def test_swap_showcase_refuses_a_lot_that_is_not_queued() -> None:
    adapter = _adapter()
    with pytest.raises(LotStateConflict):
        adapter.swap_showcase("lot_006", "lot_009", idempotency_key="k")


# ---------------------------------------------------------------------
# The injector itself
# ---------------------------------------------------------------------


def test_long_tail_latency_is_injectable() -> None:
    tail = _adapter(long_tail_rate=1.0, long_tail_ms=900)
    assert tail.markdown("lot_s01", 170.0, idempotency_key="k").latency_ms >= 900
    assert tail.stats["long_tail"] == 1

    quiet = _adapter()
    assert quiet.markdown("lot_s01", 170.0, idempotency_key="k").latency_ms == 20


def test_fault_injection_is_reproducible_under_a_seed() -> None:
    def trace(seed: int) -> list[tuple[str, int | None]]:
        adapter = MockMarketplaceAdapter(
            _records(),
            faults=FaultConfig(
                seed=seed,
                long_tail_rate=0.2,
                transient_error_rate=0.2,
                lost_response_rate=0.2,
                stale_read_rate=0.2,
                read_lag_ms=30,
            ),
        )
        out: list[tuple[str, int | None]] = []
        for i in range(30):
            try:
                result = adapter.markdown(
                    "lot_s01", 184.0 - i, idempotency_key=f"k{i}"
                )
                out.append(("ok", result.latency_ms))
            except AdapterError as exc:
                out.append((type(exc).__name__, None))
            try:
                out.append(("read", adapter.read_lot("lot_s01").version))
            except AdapterError as exc:
                out.append((type(exc).__name__, None))
        return out

    assert trace(4) == trace(4)
    assert trace(4) != trace(5)
