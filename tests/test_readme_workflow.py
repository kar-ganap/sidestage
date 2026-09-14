"""The workflow the README tells a reviewer to run, actually run.

WHY. The README sat frozen at the scaffold commit for the life of the project,
still saying *"the pipeline, console, and evaluation harness are being built"*
after all three shipped. Nothing could notice, because nothing executed it.

A reviewer's first five minutes are these five calls. If one of them 404s on a
renamed route or a hardcoded id, the project reads as broken regardless of what
the rest of the code does — so the entry point gets a test like anything else.

Runs with no credential, in replay mode, which is also what the README promises.
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


def test_the_documented_workflow_runs_end_to_end(client):
    # 1 — push the real recorded show chat through the cascade
    r = client.post("/api/replay", json={"n": 40, "source": "triage_test"})
    assert r.status_code == 200
    stats = r.json()["stats"]
    assert stats["seen"] == 40
    assert stats["dropped"] > stats["surfaced"], (
        "most live chat is noise; a triage that surfaces most of it is not triage")

    # 2 — dropped messages come back alongside surfaced ones, with their reasons
    state = client.get("/api/state").json()
    assert state["queue"] and state["log"]
    assert any(m["surfaced"] is False and m["reasons"] for m in state["log"]), (
        "a dropped message with no reason is a black box, not a product surface")

    # 3 — draft. The README takes the id from the queue rather than hardcoding
    #     it, because ranking decides which card is first.
    card = state["queue"][0]["id"]
    d = client.post(f"/api/cards/{card}/draft")
    assert d.status_code == 200
    assert d.json()["verdict"] in ("pass", "repaired", "blocked")

    # 4 — a write, with a read-back and a recorded inverse
    a = client.post("/api/actions",
                    json={"action": "markdown",
                          "params": {"lot_id": "lot_s01", "new_price": 150}})
    assert a.status_code == 200 and a.json()["status"] == "verified"

    # 5 — drive the auction into a moment
    for amount in range(900, 1125, 25):
        client.post("/api/lot/lot_006/bid", json={"amount": amount})
    assert client.get("/api/state").json()["nudge"]["moment"] == "hot"

    # 6 — on-demand product research, under the 2 s budget. The README prints
    #     exactly these four fields, so they are what this asserts.
    res = client.get("/api/research/lot_007")
    assert res.status_code == 200
    rj = res.json()
    assert rj["fact_count"] >= 10
    assert rj["budget_ms"] == 2000
    assert rj["within_budget"] is True and rj["latency_ms"] < 2000

    # ...and reset means reset (B-76)
    assert client.post("/api/reset").status_code == 200
    back = client.get("/api/state").json()
    assert back["nudge"] is None and back["stats"]["seen"] == 0


def test_healthz_reports_the_mode_without_reporting_the_key(client):
    """The README promises this. A health endpoint that leaks a credential is a
    worse bug than one that does not exist."""
    body = client.get("/healthz").json()
    assert "mode" in str(body) or "live" in str(body).lower()
    blob = str(body)
    assert "sk-ant" not in blob and "api_key" not in blob.lower()


def test_the_replay_source_names_in_the_readme_exist(client):
    """`triage_test` is the one the README uses; a renamed data file would make
    the first documented call 404."""
    r = client.post("/api/replay", json={"n": 1, "source": "triage_test"})
    assert r.status_code == 200
    bad = client.post("/api/replay", json={"n": 1, "source": "../../../etc/passwd"})
    assert bad.status_code == 404 and "available" in bad.json()
