"""B-75/B-76 — the auction dynamics Suite E classifies, reachable from the app.

WHY THIS FILE EXISTS. `app/moments.py` classifies a lot as `hot`, `stalled` or
`normal` from extension count and how far the price moved after the first
extension, and `Session.nudge()` turns that into one glanceable line. Suite E
evaluates it. An adversarial review asked whether it could ever fire in the
running product.

It could not. The catalog is a frozen snapshot of a recorded show, nothing
mutated lot state, and **every lot that classifies as hot or stalled in that
snapshot is already `sold`**. The one live lot is `normal`; the four queued lots
have no bid data at all. An entire eval suite, and the nudge it backs, sat
behind a door with no handle.

`/api/replay` already pushes the show's recorded *chat* through the cascade.
`POST /api/lot/{id}/bid` is the same idea for its *bids* — the half of the
recording the demo was ignoring.

These tests assert through the API, so they fail if the route goes away or the
session stops owning mutable lot state — which `tests/test_moments.py`, which
calls `classify()` directly with hand-picked numbers, cannot see.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session import reset_session


@pytest.fixture
def client():
    reset_session()
    with TestClient(app) as c:
        yield c
    reset_session()


def _drive(c, lot: str, events: list[dict]) -> dict:
    last: dict = {}
    for e in events:
        r = c.post(f"/api/lot/{lot}/bid", json=e)
        if r.status_code == 200:
            last = r.json()
    return last


def test_a_contested_close_produces_a_hot_nudge(client):
    """Fourteen extensions and the price up 37% since the first one. This is
    lot 4's shape from the recorded show — it opened at $27 and closed at $350,
    with 92% of that movement across 24 extensions."""
    last = _drive(client, "lot_006", [{"amount": a} for a in range(900, 1200, 25)])
    assert last["nudge"]["moment"] == "hot"
    assert "ext" in last["nudge"]["text"] and "%" in last["nudge"]["text"]


def test_a_timer_that_extends_with_no_bids_is_a_stall(client):
    """`STALL_DELTA_ABS` is exactly zero — a stall is bids that stopped
    arriving, not bids that slowed down.

    This is why `amount` is optional. The first version of `bid()` conflated a
    timer extension with a bid, and since a bid must raise the price, `delta`
    could never reach zero and `stalled` stayed unreachable even after lot state
    became mutable. Recorded lot_003 shows the real shape: 3 extensions with the
    bid frozen at $111.
    """
    last = _drive(client, "lot_007", [{"amount": 400.0}] + [{} for _ in range(3)])
    assert last["current_bid"] == 400.0
    assert last["bid_at_first_extension"] == 400.0
    assert last["nudge"]["moment"] == "stalled"


def test_an_ordinary_close_says_nothing(client):
    """Nothing is the common case, and a nudge that fires on an ordinary close
    is noise on a surface the operator can only glance at."""
    last = _drive(client, "lot_007", [{"amount": 400.0}, {"amount": 420.0}])
    assert last["nudge"] is None


def test_a_bid_that_does_not_raise_is_refused(client):
    """The room sees the standing maximum; a lower number is not a bid."""
    client.post("/api/lot/lot_006/bid", json={"amount": 900.0})
    r = client.post("/api/lot/lot_006/bid", json={"amount": 895.0})
    assert r.status_code == 409


def test_a_bin_lot_cannot_take_a_bid(client):
    """Format is load-bearing, not cosmetic (primer §6)."""
    assert client.post("/api/lot/lot_s01/bid", json={"amount": 10.0}).status_code == 409


def test_reset_puts_the_lots_back(client):
    """B-76. `get_catalog()` is a process-wide singleton whose docstring said
    "never patched" — true until `bid()` made lot state mutable. `/api/reset`
    rebuilt the Session and left the catalog carrying every bid from the
    previous run, so a reviewer who reset got a lot at $1,175 with 18 extensions
    and a permanently hot nudge."""
    _drive(client, "lot_006", [{"amount": a} for a in range(900, 1200, 25)])
    hot = client.get("/api/state").json()
    assert hot["nudge"] is not None
    client.post("/api/reset")
    back = client.get("/api/state").json()
    assert back["nudge"] is None
    assert back["lot"]["current_bid"] == 890.0


def test_the_nudge_reaches_the_console(client):
    """`/api/state` is what the console renders; a moment nobody can see is not
    a product surface."""
    _drive(client, "lot_006", [{"amount": a} for a in range(900, 1200, 25)])
    state = client.get("/api/state").json()
    assert state["nudge"]["moment"] == "hot"
    assert state["lot"]["current_bid"] == 1175.0
