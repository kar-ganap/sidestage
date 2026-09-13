"""D-21 — one write action's whole life, and how to undo it.

    propose -> precondition snapshot -> confirm -> execute (idempotency key)
            -> read-back verify -> journal with inverse op

Four properties this exists to provide, each of which is a bug if absent:

**A double-click must not double-apply a markdown.** The idempotency key is
derived from the ledger entry id, so every retry of the *same* entry carries the
same key and the adapter replays its original result. Generating a fresh key per
attempt would make retry-safety a property of luck.

**A marketplace that returns 500 after partially applying must be detectable.**
`ResponseLost` means the write may well have landed. Retrying blind is wrong and
giving up is wrong; retrying *with the same key* is the only correct move, and
the adapter's idempotency store answers whether it landed.

**The inverse op is recorded at journal time, not derived later.** Deriving a
compensating action after the fact needs the prior state, which by then may be
gone — someone else may have moved the price twice. `WriteResult.previous`
exists for exactly this, and it is captured in the same breath as the write.

**Compensating actions, not "undo".** Re-pushing the previously-live lot is a
*new* write with its own entry, its own key and its own read-back. It is not a
rewind, and the journal shows both actions because both happened.

WHAT THE READ-BACK IS FOR. A write that returns success and a read that
disagrees is the failure real marketplaces actually produce — eventual
consistency, a partial apply, a replica lagging. `verified` and `executed` are
therefore different states: executed means the API said yes, verified means we
looked. Divergence is flagged rather than retried, because retrying a write that
may have landed is how you double-apply.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.actions.adapter import (
    AdapterError,
    MarketplaceAdapter,
    PermanentError,
    RateLimited,
    ResponseLost,
    TransientError,
    WriteResult,
)
from app.models import ActionType, LedgerEntry

log = logging.getLogger("sidestage.ledger")

# Statuses, spelled out because they are a state machine rather than labels.
PROPOSED, CONFIRMED, EXECUTED = "proposed", "confirmed", "executed"
VERIFIED, DIVERGED, FAILED, ROLLED_BACK = "verified", "diverged", "failed", "rolled_back"

TERMINAL = {VERIFIED, DIVERGED, FAILED, ROLLED_BACK}

# Which actions are consequential, from D-04b. `push_lot` makes a lot live and
# ends whatever was live; on a real show that cannot be undone, only compensated
# for. `swap_showcase` reorders a queue and genuinely restores.
REVERSIBLE: dict[ActionType, bool] = {
    ActionType.PUSH_LOT: False,
    ActionType.SWAP_SHOWCASE: True,
    # Not reversible, and the reason is commercial before it is technical (B-27).
    # A markdown is a PUBLIC COMMITMENT: a buyer who saw $75 and comes back to
    # $100 was shown a price that was then withdrawn. Putting it back is a
    # second, different commercial act — which is exactly what D-21 means by
    # "compensating actions, not undo", applied more strictly than this table
    # first applied it.
    #
    # The adapter agrees independently: `markdown` refuses `new_price >=
    # current`, so D-04's four actions contain no way to raise a price at all.
    # An "inverse" that no action can execute is not an inverse.
    ActionType.MARKDOWN: False,
    # Reversible: `adjust_quantity` accepts any value at or above `committed`,
    # so restoring the prior stock level is a write the vocabulary can express.
    ActionType.ADJUST_QUANTITY: True,
}


class LedgerError(Exception):
    """A misuse of the ledger itself, distinct from a marketplace failure."""


class NotConfirmed(LedgerError):
    """Execution attempted on an entry the operator has not confirmed.

    Its own type because it is the one failure that means the *product* let
    something through, not that the marketplace misbehaved (D-22: the ladder
    changes who presses confirm, never whether confirm happens).
    """


# =====================================================================
# Store
# =====================================================================


SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    action          TEXT NOT NULL,
    params          TEXT NOT NULL,
    preconditions   TEXT NOT NULL,
    status          TEXT NOT NULL,
    inverse         TEXT,
    result          TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    executed_at     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    compensates     TEXT
);
CREATE INDEX IF NOT EXISTS ledger_status ON ledger(status);
"""


class LedgerStore:
    """SQLite, because writes are the one thing that must survive a crash.

    D-33 puts reads in memory and writes here. The database is ephemeral (D-31)
    — reseeded at boot so every reviewer gets identical state — but *within* a
    session the journal has to be durable, because the whole point is to know
    what was applied when something goes wrong mid-show.

    `idempotency_key UNIQUE` is the database enforcing what the code promises.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def put(self, e: LedgerEntry, *, attempts: int = 0,
            compensates: str | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO ledger (id, idempotency_key, action, params,"
            " preconditions, status, inverse, result, error, created_at,"
            " executed_at, attempts, compensates)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.id, e.idempotency_key, e.action.value, json.dumps(e.params),
             json.dumps(e.preconditions), e.status,
             json.dumps(e.inverse) if e.inverse else None,
             json.dumps(e.result) if e.result else None, e.error,
             (e.created_at or datetime.now(UTC)).isoformat(),
             e.executed_at.isoformat() if e.executed_at else None,
             attempts, compensates))
        self.db.commit()

    def get(self, entry_id: str) -> LedgerEntry | None:
        r = self.db.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(r) if r else None

    def all(self) -> list[LedgerEntry]:
        return [_row_to_entry(r) for r in
                self.db.execute("SELECT * FROM ledger ORDER BY created_at DESC")]

    def unfinished(self) -> list[LedgerEntry]:
        """Entries that never reached a terminal state.

        What a reconciler reads on restart: anything left `executed` was applied
        but never confirmed by a read-back, and anything left `confirmed` may or
        may not have been sent.
        """
        q = ",".join("?" * len(TERMINAL))
        return [_row_to_entry(r) for r in self.db.execute(
            f"SELECT * FROM ledger WHERE status NOT IN ({q}) ORDER BY created_at", tuple(TERMINAL))]

    def attempts(self, entry_id: str) -> int:
        r = self.db.execute("SELECT attempts FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        return int(r["attempts"]) if r else 0


def _row_to_entry(r: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        id=r["id"], idempotency_key=r["idempotency_key"],
        action=ActionType(r["action"]), params=json.loads(r["params"]),
        preconditions=json.loads(r["preconditions"]), status=r["status"],
        inverse=json.loads(r["inverse"]) if r["inverse"] else None,
        result=json.loads(r["result"]) if r["result"] else None,
        error=r["error"],
        created_at=datetime.fromisoformat(r["created_at"]),
        executed_at=datetime.fromisoformat(r["executed_at"]) if r["executed_at"] else None,
    )


# =====================================================================
# The ledger
# =====================================================================


@dataclass
class Outcome:
    """What the console renders after an execute."""

    entry: LedgerEntry
    ok: bool
    diverged: dict[str, Any] | None = None   # field -> {expected, actual}
    message: str = ""


class Ledger:
    def __init__(self, adapter: MarketplaceAdapter, *,
                 store: LedgerStore | None = None, max_attempts: int = 3) -> None:
        self.adapter = adapter
        self.store = store or LedgerStore()
        self.max_attempts = max_attempts

    # -- 1. propose ------------------------------------------------------

    def propose(self, action: ActionType, params: dict[str, Any]) -> LedgerEntry:
        """Snapshot the preconditions and mint the key. Nothing is sent.

        The key is minted **here**, once, and reused by every attempt. That is
        what makes a double-click safe: the second click carries the key the
        first one used, and the adapter replays rather than re-applies.
        """
        entry_id = f"L{uuid.uuid4().hex[:10]}"
        entry = LedgerEntry(
            id=entry_id,
            idempotency_key=f"idem_{entry_id}",
            action=action,
            params=dict(params),
            preconditions=self._snapshot(action, params),
            status=PROPOSED,
            created_at=datetime.now(UTC),
        )
        self.store.put(entry)
        return entry

    def _snapshot(self, action: ActionType, params: dict[str, Any]) -> dict[str, Any]:
        """What the world looked like when the operator was asked.

        Kept so a stale confirmation is detectable: if the operator sat on the
        dialog while the lot sold, the snapshot and the live read disagree and
        we can say so instead of writing into a changed world.
        """
        snap: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds")}
        for key in ("lot_id", "lot_id_a", "lot_id_b"):
            lot_id = params.get(key)
            if not lot_id:
                continue
            try:
                v = self.adapter.read_lot(lot_id)
            except AdapterError as exc:
                snap[lot_id] = {"unreadable": type(exc).__name__}
                continue
            snap[lot_id] = {"status": v.status, "price": v.price,
                            "quantity": v.quantity, "position": v.position,
                            "version": v.version}
        return snap

    # -- 2. confirm ------------------------------------------------------

    def confirm(self, entry_id: str) -> LedgerEntry:
        e = self._require(entry_id)
        if e.status != PROPOSED:
            raise LedgerError(f"{entry_id} is {e.status}, not {PROPOSED}")
        e.status = CONFIRMED
        self.store.put(e)
        return e

    # -- 3. execute, 4. read back, 5. journal ----------------------------

    def execute(self, entry_id: str) -> Outcome:
        """Send it, read it back, and journal the inverse.

        Retries are bounded and every one reuses the original key. A
        `ResponseLost` is retried for exactly that reason: the write may have
        landed, and the same key is the only way to find out without risking a
        second application.
        """
        e = self._require(entry_id)
        if e.status == VERIFIED:
            return Outcome(e, True, message="already verified; nothing re-sent")
        if e.status not in (CONFIRMED, EXECUTED):
            raise NotConfirmed(f"{entry_id} is {e.status}; confirm before executing")

        attempts = self.store.attempts(entry_id)
        while attempts < self.max_attempts:
            attempts += 1
            try:
                res = self._call(e)
            except RateLimited as exc:
                log.warning("ledger %s rate limited, retry after %dms",
                            entry_id, exc.retry_after_ms)
                self.store.put(e, attempts=attempts)
                continue
            except ResponseLost:
                # The write may have applied. Retrying with the SAME key is the
                # only safe move — the adapter's idempotency store will replay
                # the original result if it did land.
                log.warning("ledger %s response lost; retrying with the same key", entry_id)
                self.store.put(e, attempts=attempts)
                continue
            except TransientError as exc:
                log.warning("ledger %s transient %s", entry_id, type(exc).__name__)
                self.store.put(e, attempts=attempts)
                continue
            except PermanentError as exc:
                e.status, e.error = FAILED, f"{type(exc).__name__}: {exc}"
                e.executed_at = datetime.now(UTC)
                self.store.put(e, attempts=attempts)
                return Outcome(e, False, message=e.error)

            # Journal the inverse in the same breath as the write (D-21).
            e.status = EXECUTED
            e.executed_at = datetime.now(UTC)
            e.result = {"applied": res.applied, "versions": res.versions,
                        "replayed": res.replayed, "latency_ms": res.latency_ms}
            e.inverse = self._inverse(e.action, res)
            self.store.put(e, attempts=attempts)

            diverged = self._read_back(e, res)
            if diverged:
                e.status = DIVERGED
                e.error = f"read-back disagreed on {', '.join(diverged)}"
                self.store.put(e, attempts=attempts)
                return Outcome(e, False, diverged=diverged,
                               message="wrote successfully, read back different — "
                                       "not retried, because a retry could double-apply")
            e.status = VERIFIED
            self.store.put(e, attempts=attempts)
            return Outcome(e, True, message="replayed" if res.replayed else "applied")

        e.status, e.error = FAILED, f"gave up after {attempts} attempts"
        self.store.put(e, attempts=attempts)
        return Outcome(e, False, message=e.error)

    def _call(self, e: LedgerEntry) -> WriteResult:
        p, k = e.params, e.idempotency_key
        if e.action is ActionType.PUSH_LOT:
            return self.adapter.push_lot(p["lot_id"], idempotency_key=k)
        if e.action is ActionType.SWAP_SHOWCASE:
            return self.adapter.swap_showcase(p["lot_id_a"], p["lot_id_b"],
                                              idempotency_key=k)
        if e.action is ActionType.MARKDOWN:
            return self.adapter.markdown(p["lot_id"], float(p["new_price"]),
                                         idempotency_key=k)
        if e.action is ActionType.ADJUST_QUANTITY:
            return self.adapter.adjust_quantity(p["lot_id"], int(p["new_quantity"]),
                                                idempotency_key=k)
        raise LedgerError(f"no adapter call for {e.action}")

    def _read_back(self, e: LedgerEntry, res: WriteResult) -> dict[str, Any] | None:
        """Did the marketplace actually end up where it said it did?

        Only fields the write claims to have changed are compared. Comparing
        everything would flag a concurrent bid as our divergence, which would
        train the operator to ignore the warning.
        """
        try:
            v = self.adapter.read_lot(res.lot_id)
        except AdapterError as exc:
            return {"read_lot": {"expected": "readable", "actual": type(exc).__name__}}
        live = {"price": v.price, "quantity": v.quantity,
                "status": v.status, "position": v.position}
        out: dict[str, Any] = {}
        for field, expected in res.applied.items():
            if field not in live:
                continue
            actual = live[field]
            if isinstance(expected, float) or isinstance(actual, float):
                same = abs(float(expected or 0) - float(actual or 0)) < 1e-6
            else:
                same = expected == actual
            if not same:
                out[field] = {"expected": expected, "actual": actual}
        return out or None

    def _inverse(self, action: ActionType, res: WriteResult) -> dict[str, Any]:
        """The compensating action, built from `previous` while we still have it.

        Note what this is *not*: a rewind. `push_lot` is marked irreversible
        (D-04b) because the outgoing lot ended — re-pushing it is a new sale
        attempt on a lot that already closed, and the ledger says so rather than
        offering a button that pretends otherwise.
        """
        prev = res.previous or {}
        inv: dict[str, Any] = {"reversible": REVERSIBLE.get(action, False)}
        if action is ActionType.MARKDOWN:
            # `previous` is still recorded even though nothing can apply it: the
            # operator and the reconciler both need to know what the price was,
            # and "we cannot undo this, here is what it was" is a more useful
            # journal entry than silence.
            inv |= {"action": None,
                    "why": "a markdown is a public commitment; restoring the "
                           "price is a new commercial decision, and no action in "
                           "D-04 can raise a price"}
        elif action is ActionType.ADJUST_QUANTITY and "quantity" in prev:
            inv |= {"action": ActionType.ADJUST_QUANTITY.value,
                    "params": {"lot_id": res.lot_id, "new_quantity": prev["quantity"]}}
        elif action is ActionType.SWAP_SHOWCASE:
            ids = [k for k in res.versions if k != res.lot_id]
            inv |= {"action": ActionType.SWAP_SHOWCASE.value,
                    "params": {"lot_id_a": res.lot_id,
                               "lot_id_b": ids[0] if ids else res.lot_id}}
        else:
            inv |= {"action": None,
                    "why": "consequential: the outgoing lot ended and cannot be "
                           "restored by re-pushing it (D-04b)"}
        inv["previous"] = prev
        return inv

    # -- 6. compensate ---------------------------------------------------

    def compensate(self, entry_id: str) -> Outcome:
        """Apply the recorded inverse as a NEW entry.

        Deliberately not `rollback()`. The original still happened, the journal
        still shows it, and the compensating write gets its own idempotency key
        and its own read-back — because it is a write like any other and can
        fail like any other.
        """
        e = self._require(entry_id)
        if e.status not in (VERIFIED, DIVERGED):
            raise LedgerError(f"{entry_id} is {e.status}; nothing to compensate")
        inv = e.inverse or {}
        if not inv.get("reversible") or not inv.get("action"):
            raise LedgerError(
                f"{e.action.value} is not reversible — "
                f"{inv.get('why', 'no inverse was recorded')}")
        comp = self.propose(ActionType(inv["action"]), inv["params"])
        self.store.put(comp, compensates=entry_id)
        self.confirm(comp.id)
        out = self.execute(comp.id)
        if out.ok:
            e.status = ROLLED_BACK
            self.store.put(e, attempts=self.store.attempts(entry_id))
        return out

    # -- helpers ---------------------------------------------------------

    def _require(self, entry_id: str) -> LedgerEntry:
        e = self.store.get(entry_id)
        if e is None:
            raise LedgerError(f"no ledger entry {entry_id}")
        return e

    def journal(self) -> list[LedgerEntry]:
        return self.store.all()
