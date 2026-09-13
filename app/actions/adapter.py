"""The marketplace seam — the layer the action ledger writes through (D-24).

**This is a mock, not an integration.** Nothing here talks to eBay or Whatnot.
It is a hand-written simulation of a marketplace write API, shaped against the
real endpoints so that the integration story is concrete and so that the
reliability work above it (D-21's ledger and its reconciler) has something real
to be reliable *against*. An adapter that only ever succeeded would make that
work undemonstrable, which is the whole of D-24.

What each operation stands in for
---------------------------------

``push_lot``
    The live show-runner control: "run this lot now". eBay Live and Whatnot both
    expose this to the host in their seller UI; neither publishes a documented
    endpoint for it today, so this is the seam a partner integration would land
    on, shaped like ``POST /commerce/live/v1/show/{show_id}/lot/{lot_id}:activate``.

``swap_showcase``
    The same show-runner surface, reordering the queue rather than activating:
    ``PUT /commerce/live/v1/show/{show_id}/queue {"lot_ids": [...]}``.

``markdown``
    eBay Sell Inventory API — ``PUT /sell/inventory/v1/offer/{offerId}`` with a
    revised ``pricingSummary.price``; the legacy Trading API equivalent is
    ``ReviseFixedPriceItem`` → ``StartPrice``. eBay also has a genuine markdown
    surface in the Marketing API (item price markdown *promotions*), but that is
    a campaign over many listings; the operator here is repricing one lot mid
    show, so the offer revise is the honest analogue.

``adjust_quantity``
    ``PUT /sell/inventory/v1/inventory_item/{sku}``, field
    ``availability.shipToLocationAvailability.quantity`` (legacy:
    ``ReviseFixedPriceItem`` → ``Quantity``).

``read_lot``
    ``GET /sell/inventory/v1/offer/{offerId}`` joined with
    ``GET /sell/inventory/v1/inventory_item/{sku}``; on the live surface, the
    show's own lot state. This exists for read-back verification — D-21's
    ledger does not trust an HTTP 200, it re-reads.

A note on idempotency keys
--------------------------
eBay's write APIs do not offer a Stripe-style ``Idempotency-Key`` header. The
mock does, because D-21 requires the guarantee; on a real integration the same
guarantee has to be *reconstructed* from the ledger plus read-back, which is
precisely why the ledger journals a key and verifies by reading rather than by
trusting the response. Modelling the ideal here and the reconstruction above it
keeps the seam in the right place.

Auctions are read-only (D-03): ``markdown`` and ``adjust_quantity`` refuse an
AUCTION lot outright. ``push_lot`` and ``swap_showcase`` do not — running a lot
or reordering the queue does not touch bid state, and D-34's headline case
("lot 331 has 6 pre-bids, pull it forward") is a push of an auction lot.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from app.models import ActionType, Lot, LotFormat

# =====================================================================
# Errors
#
# The split that matters to the caller is not "which rule broke" but
# "may I retry this?". TransientError means the request may or may not
# have landed and the correct response is to replay the SAME idempotency
# key. PermanentError means replaying changes nothing — the ledger should
# journal a failure and surface it (D-23), never spin.
# =====================================================================


class AdapterError(Exception):
    """Anything the marketplace seam can raise."""


class TransientError(AdapterError):
    """Retry me, with the same idempotency key."""


class PermanentError(AdapterError):
    """Retrying will produce the same answer. Journal it and tell the operator."""


class RateLimited(TransientError):
    """HTTP 429. `retry_after_ms` is derived from the bucket, not a constant —
    it is a number the caller can actually sleep on."""

    def __init__(self, retry_after_ms: int) -> None:
        super().__init__(f"rate limited; retry after {retry_after_ms} ms")
        self.retry_after_ms = retry_after_ms


class UpstreamUnavailable(TransientError):
    """HTTP 5xx raised *before* anything was applied. Safe to retry blind."""


class ResponseLost(TransientError):
    """The write applied server-side and the response never came back.

    This is the failure idempotency exists for, and the only one where the
    caller's belief and the marketplace's state disagree. Deliberately does not
    carry the result it lost: the caller has to either replay the key or read
    back to find out, which is exactly what the reconciler does.
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            f"response lost for key {idempotency_key!r}; "
            "the write may have applied — replay the key or read back"
        )
        self.idempotency_key = idempotency_key


class LotNotFound(PermanentError):
    """HTTP 404."""


class IdempotencyKeyReuse(PermanentError):
    """Same key, different request. Real APIs return 422 here and so do we.

    Worth having: it catches a caller deriving keys too coarsely — one key per
    lot instead of one per proposal — which would silently swallow the second,
    genuinely different write.
    """


class PreconditionFailed(PermanentError):
    """A domain rule refused the write. `overridable` says whether the operator
    is even allowed to be offered an override."""

    overridable: bool = False


class FormatNotWritable(PreconditionFailed):
    """D-03. Price and stock writes apply to BIN lots only — you cannot lower a
    bid, and quantity is meaningless on a single-card auction lot."""

    def __init__(self, lot_id: str, lot_format: LotFormat, action: ActionType) -> None:
        super().__init__(
            f"{action.value} is not legal on a {lot_format.value} lot ({lot_id})"
        )
        self.lot_id = lot_id
        self.format = lot_format
        self.action = action


class LotStateConflict(PreconditionFailed):
    """The lot is in the wrong state for this write — pushing a lot that already
    sold, reordering one that is live."""

    def __init__(self, lot_id: str, status: str, wanted: str) -> None:
        super().__init__(f"lot {lot_id} is {status!r}; this action needs {wanted!r}")
        self.lot_id = lot_id
        self.status = status


class InvalidPrice(PreconditionFailed):
    """A non-positive price, or a "markdown" that raises the price.

    The direction check is not pedantry: the floor guards below only make sense
    downward, so an upward move is a different action with different
    preconditions — and it is not one of D-04's four.
    """

    def __init__(
        self, lot_id: str, requested: float, current: float | None, why: str
    ) -> None:
        super().__init__(f"price {requested} refused on {lot_id}: {why}")
        self.lot_id = lot_id
        self.requested = requested
        self.current = current


class FloorPriceViolation(PreconditionFailed):
    """The seller's own `floor_price` — a margin floor on stock they own.

    Overridable. Selling below it is a bad trade, and a seller is allowed to
    make a bad trade on their own inventory with their eyes open.
    """

    overridable = True

    def __init__(self, lot_id: str, requested: float, floor: float) -> None:
        super().__init__(
            f"markdown to {requested} on {lot_id} breaches floor_price {floor}"
        )
        self.lot_id = lot_id
        self.requested = requested
        self.floor = floor


class ConsignorFloorViolation(PreconditionFailed):
    """The consignor's floor (D-04, amended 2026-09-12). Absolute.

    Deliberately a SIBLING of `FloorPriceViolation` and not a subclass. If it
    inherited, `except FloorPriceViolation` would quietly catch it and the
    override path written for the ordinary floor would apply to a floor that
    must never be overridden. The seller does not own this stock:

        "They are not mine. I can't take an offer like that when they are not
         mine. I have been instructed to consign them and that is what I am
         doing."

    Breaching it is a breach of agreement, not a bad trade, and the caller has
    to be able to tell the two apart by type.
    """

    overridable = False

    def __init__(self, lot_id: str, requested: float, floor: float) -> None:
        super().__init__(
            f"markdown to {requested} on {lot_id} breaches consignor_floor {floor}; "
            "this floor is not the seller's to move"
        )
        self.lot_id = lot_id
        self.requested = requested
        self.consignor_floor = floor


class InvalidQuantity(PreconditionFailed):
    """Negative stock is not a state a marketplace can hold."""

    def __init__(self, lot_id: str, requested: int) -> None:
        super().__init__(f"quantity {requested} refused on {lot_id}: must be >= 0")
        self.lot_id = lot_id
        self.requested = requested


class OversellRisk(PreconditionFailed):
    """D-04's oversell guard: quantity cannot drop below what is already
    promised to buyers. Selling something twice on a live show is the failure
    the seller cannot talk their way out of."""

    def __init__(self, lot_id: str, requested: int, committed: int) -> None:
        super().__init__(
            f"quantity {requested} on {lot_id} is below {committed} already committed"
        )
        self.lot_id = lot_id
        self.requested = requested
        self.committed = committed


# =====================================================================
# Records and results
# =====================================================================


@dataclass
class LotRecord:
    """The marketplace's own row for one lot.

    Deliberately not `Lot`. Two reasons, and the second is the important one.
    D-04's consignment amendment gives lots `consigned` and `consignor_floor`,
    which `app/models.py` does not carry. And the marketplace holding its *own*
    copy, with its own version counter, is the entire reason a read-back can
    disagree with what we think we wrote.
    """

    lot_id: str
    listing_id: str
    format: LotFormat
    status: str = "queued"               # queued | live | sold | ended | shop
    position: int = 0
    price: float | None = None
    quantity: int = 0
    floor_price: float | None = None     # the seller's margin floor — overridable
    consigned: bool = False
    consignor_floor: float | None = None  # absolute (D-04)
    committed: int = 0                   # units already promised; the oversell guard
    version: int = 1                     # bumps on every write; the read-back handle

    @classmethod
    def from_lot(
        cls,
        lot: Lot,
        *,
        consigned: bool = False,
        consignor_floor: float | None = None,
        committed: int = 0,
    ) -> LotRecord:
        """Seed a row from the catalog's `Lot`, plus the consignment fields that
        live outside it."""
        return cls(
            lot_id=lot.id,
            listing_id=lot.listing_id,
            format=lot.format,
            status=lot.status,
            position=lot.position,
            price=lot.price,
            quantity=lot.quantity,
            floor_price=lot.floor_price,
            consigned=consigned,
            consignor_floor=consignor_floor,
            committed=committed,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LotRecord:
        """Seed a row straight from a `data/catalog.json` lot, which may carry
        `consigned` and `consignor_floor` keys the `Lot` dataclass does not."""
        return cls(
            lot_id=raw["id"],
            listing_id=raw.get("listing_id", ""),
            format=LotFormat(raw.get("format", "bin")),
            status=raw.get("status", "queued"),
            position=int(raw.get("position", 0)),
            price=raw.get("price"),
            quantity=int(raw.get("quantity", 0)),
            floor_price=raw.get("floor_price"),
            consigned=bool(raw.get("consigned", False)),
            consignor_floor=raw.get("consignor_floor"),
            committed=int(raw.get("committed", 0)),
        )


@dataclass(frozen=True)
class LotView:
    """What a read returns: a snapshot, with no flag saying how fresh it is.

    That omission is the point. A real API does not label a stale read — the
    caller detects lag by comparing `version` against the `versions` a write
    reported, the way an ETag or a listing revision number is used. Handing out
    an `is_stale` boolean would let the reconciler cheat.
    """

    lot_id: str
    format: LotFormat
    status: str
    position: int
    price: float | None
    quantity: int
    floor_price: float | None
    consigned: bool
    consignor_floor: float | None
    committed: int
    version: int
    observed_at_ms: int = 0


@dataclass(frozen=True)
class WriteResult:
    """A write that landed, as far as the caller can tell.

    `previous` exists so the ledger can journal the inverse op at journal time
    rather than derive it later (D-21) — after the fact, the state needed to
    build a compensating action may be gone. `versions` covers every lot the
    write touched, because a push moves two lots and a swap moves two lots.
    """

    action: ActionType
    lot_id: str                          # the primary subject
    idempotency_key: str
    applied: dict[str, Any]              # what the marketplace holds now
    previous: dict[str, Any]             # what it held before -> the inverse op
    versions: dict[str, int]             # lot_id -> version after the write
    latency_ms: int = 0
    replayed: bool = False               # served from the idempotency store


# =====================================================================
# Fault injection
# =====================================================================


@dataclass(frozen=True)
class FaultConfig:
    """Every fault mode, all off by default, all deterministic under `seed`.

    Defaults are a clean marketplace on purpose: a test opts into exactly the
    fault it is about, so a failure names its own cause.
    """

    seed: int = 0

    # Latency. Normal body plus an occasional long tail.
    latency_ms: int = 40
    latency_jitter_ms: int = 12
    long_tail_rate: float = 0.0
    long_tail_ms: int = 900

    # Transient 5xx, raised before anything is applied.
    transient_error_rate: float = 0.0

    # Rate limiting. A token bucket, so retry_after is a real derived number.
    rate_limit_per_window: int = 0        # 0 disables
    rate_limit_window_ms: int = 1_000

    # Eventual consistency: how long a write takes to reach readers.
    read_lag_ms: int = 0

    # Stale reads: probability a read is served one version behind, even after
    # the consistency window has closed.
    stale_read_rate: float = 0.0

    # Partial application: the write lands, the response does not.
    lost_response_rate: float = 0.0


@dataclass
class VirtualClock:
    """Time the adapter controls.

    Injected latency advances this clock instead of blocking, so a test can
    exercise a 900 ms tail and a 500 ms consistency window in microseconds and
    still get the real ordering. Flip `real_time` and the same latencies are
    actually felt — which is what the demo console wants.
    """

    ms: int = 0
    real_time: bool = False

    def now_ms(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += max(0, ms)

    def sleep(self, ms: int) -> None:
        self.advance(ms)
        if self.real_time and ms > 0:
            time.sleep(ms / 1000.0)


@dataclass(frozen=True)
class _Dice:
    """One roll per call. Every die is drawn every time, in this fixed order,
    even for faults that are switched off — otherwise enabling one fault
    reshuffles the RNG stream for all the others and a seeded test stops being
    reproducible when the config changes."""

    latency: float
    long_tail: float
    transient: float
    lost: float
    stale: float


@dataclass(frozen=True)
class _Recorded:
    """One idempotency-store entry: the request's fingerprint plus the outcome
    the marketplace committed to for that key."""

    fingerprint: str
    result: WriteResult | None = None
    error: PermanentError | None = None


# =====================================================================
# The protocol
# =====================================================================


@runtime_checkable
class MarketplaceAdapter(Protocol):
    """The four write actions of D-04, plus the read D-21 verifies against.

    Every write takes an `idempotency_key` and every write is replay-safe: the
    same key returns the original result without re-applying. Writes raise on
    failure rather than returning a status, so "it failed" and "it succeeded
    with a falsy result" can never be confused.
    """

    def push_lot(self, lot_id: str, *, idempotency_key: str) -> WriteResult:
        """Make `lot_id` the live lot; demote whatever was live."""
        ...

    def swap_showcase(
        self, lot_id_a: str, lot_id_b: str, *, idempotency_key: str
    ) -> WriteResult:
        """Exchange the queue positions of two queued lots."""
        ...

    def markdown(
        self, lot_id: str, new_price: float, *, idempotency_key: str
    ) -> WriteResult:
        """Lower a BIN lot's price. Guarded by both floors."""
        ...

    def adjust_quantity(
        self, lot_id: str, new_quantity: int, *, idempotency_key: str
    ) -> WriteResult:
        """Set a BIN lot's available stock. Guarded against oversell."""
        ...

    def read_lot(self, lot_id: str) -> LotView:
        """Read a lot back. May lag a write; may be one version stale."""
        ...


# =====================================================================
# The mock
# =====================================================================


class MockMarketplaceAdapter:
    """An adversarial marketplace (D-24).

    Six fault modes, each modelling something a real marketplace does:

    1. **Injected latency, normal body.**  Network plus upstream service time.
       Matters because it is charged against D-09's draft budget.
    2. **Injected latency, long tail.**  The occasional multi-hundred-ms
       response: a cold cache upstream, a GC pause, a cross-region hop. Tail
       latency, not mean latency, is what breaks a 1500 ms budget.
    3. **Transient 5xx** (`UpstreamUnavailable`).  A gateway or backend error
       raised before the write is applied — safe to retry blind.
    4. **Rate limiting** (`RateLimited`).  eBay meters applications by call
       volume; a burst of operator clicks during a hot lot is exactly when you
       hit it. Retry-after is derived from the bucket so the caller can back off
       correctly instead of guessing.
    5. **Eventual consistency and stale reads.**  A write is durable before it
       is visible, and listing reads are served by replicas. A read-back
       immediately after a write can legitimately show the old value. The caller
       must retry the read, not conclude the write failed — concluding failure
       here is how you get a double markdown.
    6. **Partial application** (`ResponseLost`).  The write is applied and the
       response is lost, so the caller sees a failure on an operation that
       actually succeeded. This is the case idempotency exists for and the case
       the reconciler has to detect: replay the key, or read back and find the
       version already moved.

    Simplification worth knowing: a replay is answered from the idempotency
    store before the handler runs, so it never re-rolls apply-time faults. A
    real server could lose the replayed response too; the store would still hold
    the answer and the next attempt would get it, so the guarantee is the same
    and the test is legible.
    """

    def __init__(
        self,
        lots: Iterable[LotRecord],
        *,
        faults: FaultConfig | None = None,
        clock: VirtualClock | None = None,
    ) -> None:
        self.faults = faults or FaultConfig()
        self.clock = clock or VirtualClock()
        self._rng = random.Random(self.faults.seed)
        self._lots: dict[str, LotRecord] = {r.lot_id: r for r in lots}
        self._idempotency: dict[str, _Recorded] = {}
        self.stats: dict[str, int] = {
            "writes_applied": 0,
            "replays": 0,
            "long_tail": 0,
            "transient_errors": 0,
            "rate_limited": 0,
            "lost_responses": 0,
            "stale_reads": 0,
        }
        # Rate-limit bucket.
        self._window_start_ms = 0
        self._window_calls = 0
        # Replica history per lot: (visible_from_ms, snapshot), oldest first.
        # Bounded by writes-per-lot in one show, which is a handful.
        self._replicas: dict[str, list[tuple[int, LotView]]] = {
            lot_id: [(0, self._snapshot(rec))] for lot_id, rec in self._lots.items()
        }

    def lot_ids(self) -> list[str]:
        """Every lot the marketplace knows about.

        A public accessor because the console needs to render the marketplace's
        own view beside ours, and reaching into `_lots` from a route makes the
        route depend on a private field.
        """
        return list(self._lots)

    # --- the four writes (D-04) ---------------------------------------

    def push_lot(self, lot_id: str, *, idempotency_key: str) -> WriteResult:
        def handler() -> tuple[dict[str, Any], dict[str, Any], list[LotRecord]]:
            target = self._require(lot_id)
            if target.status in ("sold", "ended"):
                raise LotStateConflict(lot_id, target.status, "a lot that can still run")
            was_live = next(
                (
                    r
                    for r in self._lots.values()
                    if r.status == "live" and r.lot_id != lot_id
                ),
                None,
            )
            previous = {
                "live_lot_id": was_live.lot_id if was_live else None,
                "status": target.status,
            }
            touched = [target]
            if was_live is not None:
                # Demote rather than end it. A push has to stay reversible to be
                # eligible for L3 on the automation ladder (D-22); ending the
                # outgoing lot would make it consequential and cap it at L1.
                was_live.status = "queued"
                touched.append(was_live)
            target.status = "live"
            return {"live_lot_id": lot_id, "status": "live"}, previous, touched

        return self._execute(
            ActionType.PUSH_LOT, lot_id, idempotency_key, {"lot_id": lot_id}, handler
        )

    def swap_showcase(
        self, lot_id_a: str, lot_id_b: str, *, idempotency_key: str
    ) -> WriteResult:
        def handler() -> tuple[dict[str, Any], dict[str, Any], list[LotRecord]]:
            a = self._require(lot_id_a)
            b = self._require(lot_id_b)
            for rec in (a, b):
                if rec.status != "queued":
                    raise LotStateConflict(rec.lot_id, rec.status, "queued")
            previous = {"positions": {a.lot_id: a.position, b.lot_id: b.position}}
            a.position, b.position = b.position, a.position
            applied = {"positions": {a.lot_id: a.position, b.lot_id: b.position}}
            return applied, previous, [a, b]

        # Order-independent params: swapping a with b is the same request as
        # swapping b with a, so the same key must fingerprint identically.
        params = {"lot_ids": tuple(sorted((lot_id_a, lot_id_b)))}
        return self._execute(
            ActionType.SWAP_SHOWCASE, lot_id_a, idempotency_key, params, handler
        )

    def markdown(
        self, lot_id: str, new_price: float, *, idempotency_key: str
    ) -> WriteResult:
        def handler() -> tuple[dict[str, Any], dict[str, Any], list[LotRecord]]:
            rec = self._require(lot_id)
            if rec.format is not LotFormat.BIN:
                raise FormatNotWritable(lot_id, rec.format, ActionType.MARKDOWN)  # D-03
            if new_price <= 0:
                raise InvalidPrice(lot_id, new_price, rec.price, "must be positive")
            if rec.price is not None and new_price >= rec.price:
                raise InvalidPrice(
                    lot_id, new_price, rec.price, f"not below the current price {rec.price}"
                )
            # The consignor floor is checked FIRST and keys off the floor being
            # present rather than off the `consigned` flag. Both choices fail
            # closed: a price that breaches both floors must be reported as the
            # one the operator cannot override, and a row carrying a consignor
            # floor with the flag unset is bad data we decline to price through.
            if rec.consignor_floor is not None and new_price < rec.consignor_floor:
                raise ConsignorFloorViolation(lot_id, new_price, rec.consignor_floor)
            if rec.floor_price is not None and new_price < rec.floor_price:
                raise FloorPriceViolation(lot_id, new_price, rec.floor_price)
            previous = {"price": rec.price}
            rec.price = float(new_price)
            return {"price": rec.price}, previous, [rec]

        return self._execute(
            ActionType.MARKDOWN,
            lot_id,
            idempotency_key,
            {"lot_id": lot_id, "new_price": float(new_price)},
            handler,
        )

    def adjust_quantity(
        self, lot_id: str, new_quantity: int, *, idempotency_key: str
    ) -> WriteResult:
        def handler() -> tuple[dict[str, Any], dict[str, Any], list[LotRecord]]:
            rec = self._require(lot_id)
            if rec.format is not LotFormat.BIN:
                raise FormatNotWritable(lot_id, rec.format, ActionType.ADJUST_QUANTITY)
            if new_quantity < 0:
                raise InvalidQuantity(lot_id, new_quantity)
            if new_quantity < rec.committed:
                raise OversellRisk(lot_id, new_quantity, rec.committed)
            previous = {"quantity": rec.quantity}
            rec.quantity = int(new_quantity)
            return {"quantity": rec.quantity}, previous, [rec]

        return self._execute(
            ActionType.ADJUST_QUANTITY,
            lot_id,
            idempotency_key,
            {"lot_id": lot_id, "new_quantity": int(new_quantity)},
            handler,
        )

    # --- the read (D-21's read-back verify) ----------------------------

    def read_lot(self, lot_id: str) -> LotView:
        dice = self._roll()
        self.clock.sleep(self._latency(dice))
        self._check_rate_limit()          # reads spend quota too, as they do for real
        self._maybe_transient(dice)
        if lot_id not in self._lots:
            raise LotNotFound(lot_id)
        stale = dice.stale < self.faults.stale_read_rate
        if stale:
            self.stats["stale_reads"] += 1
        return self._visible(lot_id, stale=stale)

    # --- fault injection ------------------------------------------------

    def _roll(self) -> _Dice:
        r = self._rng
        return _Dice(
            latency=r.gauss(0.0, 1.0),
            long_tail=r.random(),
            transient=r.random(),
            lost=r.random(),
            stale=r.random(),
        )

    def _latency(self, dice: _Dice) -> int:
        f = self.faults
        ms = f.latency_ms + dice.latency * f.latency_jitter_ms
        # Fault mode 2: the long tail. Models a cold upstream cache, a GC pause,
        # a cross-region hop. Tail latency is what blows a budget, not the mean.
        if dice.long_tail < f.long_tail_rate:
            ms += f.long_tail_ms
            self.stats["long_tail"] += 1
        return max(0, round(ms))

    def _check_rate_limit(self) -> None:
        # Fault mode 4: a token bucket, checked at the gateway before anything
        # else — so a 429 never applies a write and never records a key.
        f = self.faults
        if f.rate_limit_per_window <= 0:
            return
        now = self.clock.now_ms()
        if now - self._window_start_ms >= f.rate_limit_window_ms:
            self._window_start_ms = now
            self._window_calls = 0
        self._window_calls += 1
        if self._window_calls > f.rate_limit_per_window:
            self.stats["rate_limited"] += 1
            raise RateLimited(self._window_start_ms + f.rate_limit_window_ms - now)

    def _maybe_transient(self, dice: _Dice) -> None:
        # Fault mode 3: 5xx before the handler runs, so nothing is applied.
        if dice.transient < self.faults.transient_error_rate:
            self.stats["transient_errors"] += 1
            raise UpstreamUnavailable("upstream returned 503")

    # --- consistency ----------------------------------------------------

    def _snapshot(self, rec: LotRecord) -> LotView:
        return LotView(
            lot_id=rec.lot_id,
            format=rec.format,
            status=rec.status,
            position=rec.position,
            price=rec.price,
            quantity=rec.quantity,
            floor_price=rec.floor_price,
            consigned=rec.consigned,
            consignor_floor=rec.consignor_floor,
            committed=rec.committed,
            version=rec.version,
        )

    def _publish(self, rec: LotRecord) -> None:
        # Fault mode 5a: durable now, visible later. The new snapshot is queued
        # behind a visibility horizon; until the clock passes it, readers keep
        # seeing the old value even though the write is committed.
        visible_at = self.clock.now_ms() + self.faults.read_lag_ms
        self._replicas[rec.lot_id].append((visible_at, self._snapshot(rec)))

    def _visible(self, lot_id: str, *, stale: bool) -> LotView:
        now = self.clock.now_ms()
        versions = self._replicas[lot_id]
        idx = 0
        for i, (visible_at, _) in enumerate(versions):
            if visible_at <= now:
                idx = i
        # Fault mode 5b: a read served by a replica exactly one version behind,
        # even after the consistency window closed. Models a load balancer
        # routing a read to a lagging follower.
        if stale and idx > 0:
            idx -= 1
        return replace(versions[idx][1], observed_at_ms=now)

    # --- the write pipeline ---------------------------------------------

    def _require(self, lot_id: str) -> LotRecord:
        rec = self._lots.get(lot_id)
        if rec is None:
            raise LotNotFound(lot_id)
        return rec

    def _execute(
        self,
        action: ActionType,
        lot_id: str,
        idempotency_key: str,
        params: Mapping[str, Any],
        handler: Callable[[], tuple[dict[str, Any], dict[str, Any], list[LotRecord]]],
    ) -> WriteResult:
        dice = self._roll()
        latency = self._latency(dice)
        self.clock.sleep(latency)                     # fault modes 1 and 2
        self._check_rate_limit()                      # fault mode 4, at the gateway

        fingerprint = f"{action.value}:{sorted(params.items())!r}"
        recorded = self._idempotency.get(idempotency_key)
        if recorded is not None:
            if recorded.fingerprint != fingerprint:
                raise IdempotencyKeyReuse(
                    f"key {idempotency_key!r} was used for a different request"
                )
            self.stats["replays"] += 1
            if recorded.error is not None:
                # The marketplace committed to an answer for this key. A refusal
                # replays as the same refusal, or "same key, same outcome" would
                # be a claim rather than a property.
                raise recorded.error
            assert recorded.result is not None
            return replace(recorded.result, replayed=True, latency_ms=latency)

        self._maybe_transient(dice)                   # fault mode 3, before apply

        try:
            applied, previous, touched = handler()
        except PermanentError as exc:
            self._idempotency[idempotency_key] = _Recorded(fingerprint, error=exc)
            raise
        # Transient failures are deliberately NOT recorded: the request did not
        # complete, and retrying it is the entire point of the key.

        for rec in touched:
            rec.version += 1
            self._publish(rec)
        self.stats["writes_applied"] += 1

        result = WriteResult(
            action=action,
            lot_id=lot_id,
            idempotency_key=idempotency_key,
            applied=applied,
            previous=previous,
            versions={rec.lot_id: rec.version for rec in touched},
            latency_ms=latency,
        )
        # Recorded BEFORE the lost-response roll. That ordering is the whole
        # mechanism: the marketplace has already written down its answer, so the
        # caller who saw a failure can come back with the same key and find it.
        self._idempotency[idempotency_key] = _Recorded(fingerprint, result=result)

        # Fault mode 6: partial application. Applied server-side, response lost.
        if dice.lost < self.faults.lost_response_rate:
            self.stats["lost_responses"] += 1
            raise ResponseLost(idempotency_key)

        return result


def adapter_from_catalog(
    raw_lots: Sequence[Mapping[str, Any]], **kwargs: Any
) -> MockMarketplaceAdapter:
    """Build a mock seeded from `data/catalog.json`'s `lots` array."""
    return MockMarketplaceAdapter([LotRecord.from_dict(r) for r in raw_lots], **kwargs)
