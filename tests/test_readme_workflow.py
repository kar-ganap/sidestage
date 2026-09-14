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


def test_the_generated_results_page_matches_the_recorded_runs():
    """B-137. `static/results.html` is generated from `evals/results/*.json`, so
    a committed copy that no longer matches them is a results page telling a
    reviewer something the runs do not say.

    This is the same guarantee `check_docs.py` gives the prose version, applied
    to the one document that would otherwise be exempt because nothing in it was
    typed by hand.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).parent.parent
    r = subprocess.run(
        [sys.executable, "tools/render_results.py", "--check"],
        cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, (
        f"static/results.html is stale — run "
        f"`uv run python tools/render_results.py`\n{r.stdout}{r.stderr}")


def test_the_results_page_is_actually_served(client):
    """A results page the app does not serve is a file, not a link. It sits in
    `static/` so `https://<host>/results.html` works for a reviewer who never
    clones anything."""
    r = client.get("/results.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "SideStage — Results" in body
    assert "<svg" in body, "the charts are inline SVG; no CDN, per D-07"


def test_the_pr_sweep_agrees_with_the_recorded_eval():
    """Two independent computations of the same thing must agree.

    `tools/render_results.py` re-derives precision and recall by loading the
    gate and sweeping every distinct score over the 189 labelled rows.
    `evals/run_triage.py` computed A1 once and wrote `triage_189.json`. If the
    chart and the table disagree, one of them is wrong and a reviewer has no way
    to tell which — so this pins them to each other at the shipped threshold.
    """
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "rr", root / "tools" / "render_results.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)

    op = rr.pr_curve_data()["op"]
    rec = json.loads((root / "evals/results/triage_189.json").read_text())["arms"]["A1"]
    assert abs(op["p"] - 100 * rec["p"]) < 0.05, (
        f"swept precision {op['p']:.1f} vs recorded {100*rec['p']:.1f}")
    assert abs(op["r"] - 100 * rec["r"]) < 0.05, (
        f"swept recall {op['r']:.1f} vs recorded {100*rec['r']:.1f}")


def test_no_chart_label_is_drawn_outside_its_viewbox():
    """B-139. `gate @ 0.23` sits at 89% recall, which put its label 7 px past the
    right edge of a 520-wide canvas — clipped, and the chart silently lied about
    where its own operating point was. The dot plot had the same bug one pixel
    wide, on the only string that reaches it: the MUTE control scores exactly
    `100.0%`.

    Both are placement rules now (labels flip side near an edge; the margin fits
    the widest value), and this is what keeps them true. Rotated axis titles are
    excluded because they run vertically, so a horizontal extent says nothing
    about them.
    """
    import re
    from pathlib import Path

    page = (Path(__file__).parent.parent / "static/results.html").read_text(
        encoding="utf-8")
    charts = re.findall(r'<svg viewBox="0 0 (\d+) (\d+)"[^>]*>(.*?)</svg>',
                        page, re.S)
    assert charts, "no charts in the results page"
    offenders = []
    for w, h, body in charts:
        W, H = int(w), int(h)
        for m in re.finditer(r"<text ([^>]*)>([^<]*)</text>", body):
            attrs, text = m.group(1), m.group(2)
            if not text.strip() or "rotate" in attrs:
                continue
            x = float(re.search(r'x="([\d.-]+)"', attrs).group(1))
            y = float(re.search(r'y="([\d.-]+)"', attrs).group(1))
            anchor_m = re.search(r'text-anchor="(\w+)"', attrs)
            anchor = anchor_m.group(1) if anchor_m else "start"
            width = len(text) * 5.9          # ~11px sans; generous
            left = {"start": x, "middle": x - width / 2, "end": x - width}[anchor]
            if left < -1 or left + width > W + 1 or not (0 <= y <= H):
                offenders.append(f"{text!r} in {W}x{H} spans "
                                 f"{left:.0f}..{left + width:.0f}, y={y}")
    assert not offenders, "chart labels drawn outside the canvas:\n  " + \
        "\n  ".join(offenders)
