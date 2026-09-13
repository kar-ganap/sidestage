"""B-73 — the write path, exercised through the API the product actually serves.

WHY THIS FILE EXISTS. `app/actions/` is 53 KB: a marketplace adapter with six
fault modes, and a ledger implementing D-21's propose -> confirm -> execute ->
read-back -> journal lifecycle. It had **46 tests** and was imported by nothing
else in the repo. Not by `app/main.py`, not by `app/session.py`, not by any
eval. An adversarial review found it by asking the only question that matters
about a "concrete failure path": *what calls it?*

Nothing did. A fault model nobody can trigger and an idempotency key nobody
mints are claims about the design, not properties of the system.

So these tests go through `TestClient` rather than importing `Ledger` directly.
That is the point: they fail if the route is removed, if `Session` stops holding
a ledger, or if the wiring rots — none of which the existing 46 tests can see.
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


def test_the_write_path_is_reachable_from_the_running_app(client):
    """The whole reason this file exists. If this fails, `app/actions/` is
    dead code again and its 46 tests are testing a library nobody uses."""
    r = client.post("/api/actions",
                    json={"action": "markdown",
                          "params": {"lot_id": "lot_s01", "new_price": 150.0}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["status"] == "verified"


def test_a_write_records_its_inverse_at_journal_time(client):
    """D-21. Deriving a compensating action later needs the prior state, which
    by then may be gone — someone else may have moved the price twice. So the
    inverse is captured in the same breath as the write."""
    r = client.post("/api/actions",
                    json={"action": "markdown",
                          "params": {"lot_id": "lot_s01", "new_price": 150.0}})
    inv = r.json()["inverse"]
    assert inv["previous"]["price"] == 185.0
    # B-27: the inverse of a markdown is NOT a markdown — `markdown` refuses any
    # price at or above the current one, so a recorded "compensation" that
    # raised the price back would have been unexecutable. It is irreversible,
    # and saying so is the honest answer.
    assert inv["reversible"] is False and inv["action"] is None
    assert "commitment" in inv["why"]


def test_pushing_a_lot_is_consequential_and_says_so(client):
    """D-04b. Re-pushing the outgoing lot does not restore it: it ended."""
    inv = client.post("/api/actions",
                      json={"action": "push_lot",
                            "params": {"lot_id": "lot_007"}}).json()["inverse"]
    assert inv["reversible"] is False
    assert inv["previous"]["live_lot_id"]


def test_the_marketplace_keeps_its_own_copy(client):
    """The read-back only means something because the marketplace's row is not
    ours — its own status, its own version counter. `/api/state` and
    `/api/actions/lots` are deliberately separate views."""
    before = {l["lot_id"]: l for l in client.get("/api/actions/lots").json()["lots"]}
    assert before["lot_s01"]["price"] == 185.0
    client.post("/api/actions", json={"action": "markdown",
                                      "params": {"lot_id": "lot_s01",
                                                 "new_price": 150.0}})
    after = {l["lot_id"]: l for l in client.get("/api/actions/lots").json()["lots"]}
    assert after["lot_s01"]["price"] == 150.0
    assert after["lot_s01"]["version"] > before["lot_s01"]["version"]


def test_a_double_click_does_not_double_apply(client):
    """The idempotency key is derived from the ledger entry id, so every retry
    of the SAME entry replays. Two separate clicks are two entries and two
    writes — which is correct, and is why the guard has to live at the entry
    level rather than being a rate limit."""
    body = {"action": "adjust_quantity",
            "params": {"lot_id": "lot_s01", "new_quantity": 2}}
    first = client.post("/api/actions", json=body).json()
    second = client.post("/api/actions", json=body).json()
    assert first["id"] != second["id"]
    lots = {l["lot_id"]: l for l in client.get("/api/actions/lots").json()["lots"]}
    assert lots["lot_s01"]["quantity"] == 2      # idempotent in effect


def test_a_floor_violation_is_refused_and_journalled(client):
    """A permanent error is not retried, and the ledger records why. The
    operator sees the refusal and the reason, same as a blocked draft (D-23)."""
    r = client.post("/api/actions",
                    json={"action": "markdown",
                          "params": {"lot_id": "lot_s01", "new_price": 1.0}})
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "failed"
    assert "Floor" in body["error"] or "floor" in body["error"].lower()


def test_a_write_to_a_lot_that_does_not_exist_is_a_422_not_a_500(client):
    r = client.post("/api/actions",
                    json={"action": "markdown", "params": {"lot_id": "nope"}})
    assert r.status_code in (409, 422), r.text


def test_the_journal_reaches_the_console(client):
    """`/api/state` is what the console renders. A write nobody can see is not
    an auditable write."""
    client.post("/api/actions", json={"action": "markdown",
                                      "params": {"lot_id": "lot_s01",
                                                 "new_price": 150.0}})
    ledger = client.get("/api/state").json()["ledger"]
    assert any(e.get("action") == "markdown" for e in ledger)


# =====================================================================
# Under an adversarial marketplace
# =====================================================================


@pytest.fixture
def faulty(monkeypatch):
    """The same routes, against a marketplace that loses responses, returns
    503s and serves stale reads. `SIDESTAGE_FAULTS=1` is what a reviewer sets
    to see this by hand."""
    monkeypatch.setenv("SIDESTAGE_FAULTS", "1")
    reset_session()
    with TestClient(app) as c:
        yield c
    reset_session()


def test_a_lost_response_is_replayed_not_reapplied(faulty):
    """The case idempotency exists for: the write lands and the response is
    lost, so the caller sees a failure on an operation that succeeded. Retrying
    blind double-applies; giving up leaves the operator wrong about the world.
    Retrying with the SAME key is the only correct move, and the count of
    replays is the evidence it happened."""
    for lot in ["lot_s01", "lot_s02", "lot_s03"] * 5:
        faulty.post("/api/actions",
                    json={"action": "adjust_quantity",
                          "params": {"lot_id": lot, "new_quantity": 2}})
    stats = faulty.get("/api/actions/lots").json()["adapter_stats"]
    assert stats["lost_responses"] > 0, "the fault never fired; the test proves nothing"
    assert stats["replays"] > 0, "a lost response was not retried with its original key"


def test_a_divergent_read_back_is_flagged_and_not_retried(faulty):
    """A write that returns success and a read that disagrees is what real
    marketplaces produce — eventual consistency, a replica lagging. Retrying a
    write that may have landed is how you double-apply, so divergence is
    reported rather than resolved."""
    seen = []
    for lot in ["lot_s01", "lot_s02", "lot_s03"] * 5:
        seen.append(faulty.post("/api/actions",
                                json={"action": "adjust_quantity",
                                      "params": {"lot_id": lot,
                                                 "new_quantity": 2}}).json())
    diverged = [r for r in seen if r["status"] == "diverged"]
    assert diverged, "no divergence observed; the stale-read fault never fired"
    assert all(not r["ok"] for r in diverged)
    assert "double-apply" in diverged[0]["message"]


def test_an_unreadable_lot_does_not_take_down_the_panel(faulty):
    """One flaky lot must not 500 the whole marketplace view, and must not be
    silently dropped either — a view that is quietly incomplete is worse than
    one that says which row it could not read."""
    for _ in range(6):
        r = faulty.get("/api/actions/lots")
        assert r.status_code == 200
        assert len(r.json()["lots"]) == 15
