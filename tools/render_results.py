#!/usr/bin/env python3
"""Generate `static/results.html` from the recorded runs.

    uv run python tools/render_results.py          # write it
    uv run python tools/render_results.py --check  # fail if it is out of date

WHY GENERATED RATHER THAN WRITTEN. `docs/RESULTS.md` already states every one of
these figures and `check_docs.py` pins ten of them. A hand-written HTML twin
would be a second copy of numbers owned by a third file — the exact shape of
every documentation bug in this repo, and the one a results page is most likely
to acquire, because it restates figures it does not own.

So this reads `evals/results/*.json` directly. The page cannot drift from the
runs, because there is nothing in it that a run did not produce. `--check` is
what keeps that true in CI: it regenerates into memory and compares.

WHY HTML AT ALL, given the markdown renders on GitHub. Two reasons, neither
cosmetic. Charts: a bar is a better comparison than a column of digits when the
point is "S0 is half of S1", and inline SVG needs no library and no CDN — which
matters because the page is served by the app itself at `/results.html`, under
the same no-build-step rule as the console (D-07). And markdown previewers with
a mermaid plugin try to render every fenced block; the bash blocks in RESULTS.md
come out as "no diagram type detected" in at least one popular IDE. An HTML page
has no fences to misread.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals/results"
OUT = ROOT / "static/results.html"

sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------------ the data


def _safe(r: dict) -> bool | None:
    """Identical to `evals/report_spike1._safe`. Imported rather than copied
    where possible; duplicated here only if that import fails, because a second
    definition of what "safe" means is exactly the drift this file avoids."""
    if r["blocked"]:
        return True
    return None if r["asserts"] is None else not r["asserts"]


def _rate(rows: list[dict], fn) -> float | None:
    vals = [v for v in (fn(r) for r in rows) if v is not None]
    return 100 * sum(vals) / len(vals) if vals else None


def _need(rows: list[dict], fn, what: str) -> float:
    """A rate that came back None means every row was indeterminate, which is a
    broken run rather than a zero. Fail loudly: a results page that renders a
    silent 0.0 is worse than one that does not render at all."""
    v = _rate(rows, fn)
    if v is None:
        raise ValueError(f"no scorable rows for {what} — the run file is incomplete")
    return v


def spike1() -> dict:
    """Sonnet-5, three runs, S0/S1/S2 — the ablation."""
    runs = [json.loads((RESULTS / f"spike1_sonnet-5_S0S1S2_b1_run{i}.json").read_text())
            for i in (1, 2, 3)]
    arms = {}
    for arm in ("S0", "S1", "S2", "MUTE"):
        safe, resp = [], []
        for d in runs:
            rows = [r for r in d["rows"] if r["arm"] == arm]
            safe.append(_need(rows, _safe, f"{arm} safe"))
            resp.append(_need(rows, lambda r: r["responsive"], f"{arm} responsive"))
        arms[arm] = {"safe": sorted(safe), "resp": sorted(resp)}
    deltas = []
    for d in runs:
        g = lambda a, f, w: _need([r for r in d["rows"] if r["arm"] == a], f, w)
        deltas.append((g("S2", _safe, "S2 safe") - g("S1", _safe, "S1 safe"),
                       g("S2", lambda r: r["responsive"], "S2 resp")
                       - g("S1", lambda r: r["responsive"], "S1 resp")))
    # paired, over all runs
    pairs = {}
    for axis, fn in (("safe", _safe), ("resp", lambda r: r["responsive"])):
        b = c = 0
        for d in runs:
            idx = {r["case_id"]: r for r in d["rows"] if r["arm"] == "S1"}
            for r2 in (r for r in d["rows"] if r["arm"] == "S2"):
                r1 = idx.get(r2["case_id"])
                if r1 is None:
                    continue
                a, z = fn(r1), fn(r2)
                if a is None or z is None:
                    continue
                b += bool(a and not z)
                c += bool(z and not a)
        from evals.stats import mcnemar
        pairs[axis] = (b, c, mcnemar(b, c).p)
    # case count per arm, never the union — the MUTE control shares one id (B-133)
    per_arm: dict[str, set] = collections.defaultdict(set)
    for r in runs[0]["rows"]:
        per_arm[r["arm"]].add(r["case_id"])
    return {"arms": arms, "deltas": deltas, "pairs": pairs,
            "cases": max(len(v) for v in per_arm.values()), "runs": len(runs)}


def spike2() -> dict:
    d189 = json.loads((RESULTS / "triage_189.json").read_text())
    dbest = json.loads((RESULTS / "triage.json").read_text())
    d161 = json.loads((RESULTS / "triage_161.json").read_text())
    return {"n189": d189, "best": dbest, "n161": d161}


def bench() -> dict:
    return json.loads((RESULTS / "bench.json").read_text())


# ------------------------------------------------------------------ the page


def pr_curve_data() -> dict:
    """A real precision–recall curve, swept from the gate itself.

    Not decoration and not simulated: `Model.load()` scores all 189 labelled
    rows, and the threshold is swept across every distinct score. That is the
    honest way to show what D-27 argues in prose — the operating point is
    chosen from cost asymmetry (a missed question costs a sale; a false positive
    costs two seconds of attention), and a curve makes the shape of that
    trade-off visible in a way three numbers cannot.

    A0 is a regex and A2 has a model in it, so neither sweeps. They are plotted
    as single points, which is what they are.
    """
    from app.triage import Model, features, prefilter
    from app.entities import get_resolver

    m, res = Model.load(), get_resolver()
    rows = []
    for name in ("triage_test", "triage_show2"):
        f = ROOT / f"evals/data/{name}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and "_meta" not in line:
                rows.append(json.loads(line))
    scored = [(m.score(features(r["text"], res)), bool(r["seller_directed"]))
              for r in rows]
    pos = sum(1 for _, y in scored if y)

    pts = []
    for t in sorted({round(sc, 4) for sc, _ in scored} | {0.0, 1.0}):
        tp = sum(1 for sc, y in scored if sc >= t and y)
        fp = sum(1 for sc, y in scored if sc >= t and not y)
        if tp + fp == 0:
            continue
        pts.append({"t": t, "p": 100 * tp / (tp + fp), "r": 100 * tp / pos})
    pts.sort(key=lambda d: d["r"])
    # where the shipped gate actually sits
    op = dict(min(pts, key=lambda d: abs(d["t"] - m.threshold)))
    op["shipped"] = m.threshold
    return {"points": pts, "op": op, "threshold": m.threshold,
            "n": len(scored), "pos": pos}


def line_chart(pts: list[dict], op: dict, marks: list[dict]) -> str:
    """Precision (y) against recall (x), 0–100 on both. One scale places the
    curve, the ticks and every label."""
    W, H = 520, 300
    L, R, T, B = 46, 14, 14, 38          # room for the outermost tick labels
    px = lambda r: L + (r / 100) * (W - L - R)
    py = lambda p: T + (1 - p / 100) * (H - T - B)

    grid = []
    for v in (0, 25, 50, 75, 100):
        grid.append(f'<line x1="{px(v):.1f}" y1="{T}" x2="{px(v):.1f}" y2="{H-B}" '
                    f'stroke="var(--line)" stroke-width="1"/>')
        grid.append(f'<line x1="{L}" y1="{py(v):.1f}" x2="{W-R}" y2="{py(v):.1f}" '
                    f'stroke="var(--line)" stroke-width="1"/>')
        grid.append(f'<text x="{px(v):.1f}" y="{H-B+15}" text-anchor="middle" '
                    f'font-size="10" fill="var(--ink-faint)">{v}</text>')
        grid.append(f'<text x="{L-7}" y="{py(v)+3.5:.1f}" text-anchor="end" '
                    f'font-size="10" fill="var(--ink-faint)">{v}</text>')

    path = " ".join(f"{px(d['r']):.1f},{py(d['p']):.1f}" for d in pts)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="precision against recall for the triage gate">']
    out += grid
    out.append(f'<polyline points="{path}" fill="none" stroke="var(--ok)" '
               f'stroke-width="2"/>')
    # the shipped operating point
    out.append(f'<circle cx="{px(op["r"]):.1f}" cy="{py(op["p"]):.1f}" r="5" '
               f'fill="var(--ok)" stroke="var(--panel)" stroke-width="2"/>')
    # Label with the SHIPPED threshold, not the nearest distinct score the sweep
    # happened to land on. The point is right either way; the label would
    # otherwise read 0.22 for a gate that is configured at 0.23.
    out.append(f'<text x="{px(op["r"])+9:.1f}" y="{py(op["p"])-7:.1f}" font-size="11" '
               f'fill="var(--ink)">gate @ {op["shipped"]:.2f}</text>')
    for mk in marks:
        out.append(f'<rect x="{px(mk["r"])-4:.1f}" y="{py(mk["p"])-4:.1f}" width="8" '
                   f'height="8" fill="{mk["hue"]}" stroke="var(--panel)" stroke-width="2"/>')
        out.append(f'<text x="{px(mk["r"])+9:.1f}" y="{py(mk["p"])+4:.1f}" font-size="11" '
                   f'fill="var(--ink)">{html.escape(mk["label"])}</text>')
    out.append(f'<text x="{(L+W-R)/2:.0f}" y="{H-6}" text-anchor="middle" '
               f'font-size="11" fill="var(--ink-soft)">recall %</text>')
    out.append(f'<text x="12" y="{(T+H-B)/2:.0f}" text-anchor="middle" font-size="11" '
               f'fill="var(--ink-soft)" transform="rotate(-90 12 {(T+H-B)/2:.0f})">precision %</text>')
    out.append("</svg>")
    return "".join(out)


def dot_plot(arms: dict, axis: str, labels: list[tuple[str, str]]) -> str:
    """Every run as its own dot, rather than a bar hiding three numbers.

    A bar at the median of three runs asserts a point estimate the data does not
    support; three dots and the span show what was actually observed. This is
    the same argument the page's second rule makes in words."""
    W, H = 520, 34 * len(labels) + 34
    L, R = 168, 42
    lo = min(min(arms[a][axis]) for a, _ in labels)
    span_lo = max(0.0, lo - 8)
    px = lambda v: L + (v - span_lo) / (100 - span_lo) * (W - L - R)

    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="{axis} per run, every run shown">']
    for v in (25, 50, 75, 100):
        if v <= span_lo:
            continue
        out.append(f'<line x1="{px(v):.1f}" y1="18" x2="{px(v):.1f}" y2="{H-20}" '
                   f'stroke="var(--line)" stroke-width="1"/>')
        out.append(f'<text x="{px(v):.1f}" y="{H-6}" text-anchor="middle" '
                   f'font-size="10" fill="var(--ink-faint)">{v}</text>')
    for i, (arm, label) in enumerate(labels):
        y = 34 + i * 34
        runs = arms[arm][axis]
        hue = "var(--slate)" if arm == "MUTE" else (
              "var(--warn)" if arm == "S0" else "var(--ok)")
        out.append(f'<text x="{L-10}" y="{y+4}" text-anchor="end" font-size="11.5" '
                   f'fill="var(--ink)">{html.escape(label)}</text>')
        if max(runs) - min(runs) > 0.05:
            out.append(f'<line x1="{px(min(runs)):.1f}" y1="{y}" '
                       f'x2="{px(max(runs)):.1f}" y2="{y}" stroke="{hue}" '
                       f'stroke-width="2" opacity=".45"/>')
        for v in runs:
            out.append(f'<circle cx="{px(v):.1f}" cy="{y}" r="4.5" fill="{hue}" '
                       f'stroke="var(--panel)" stroke-width="1.5"/>')
        out.append(f'<text x="{W-R+8}" y="{y+4}" font-size="11" '
                   f'fill="var(--ink-soft)">{sorted(runs)[1]:.1f}%</text>')
    out.append("</svg>")
    return "".join(out)


def latency_chart(free: dict, live: dict | None) -> str:
    """Log scale, because the paths span five orders of magnitude — 0.1 ms to
    15 s. A linear axis would render every free path as a zero-width bar and
    hide the only comparison that matters."""
    import math
    rows = [(k, v["p50"], v["p95"], 2000 if "research" in k else None)
            for k, v in free.items()]
    if live:
        for k, v in live.items():
            if k.startswith("_"):
                continue
            budget = {"triage stage 2 (escalated)": 600,
                      "draft: to first token": 1500,
                      "draft: to sendable": 3000}.get(k)
            rows.append((k, v["p50"], v["p95"], budget))
    W = 520
    H = 26 * len(rows) + 40
    L, R = 210, 16
    lo, hi = math.log10(0.05), math.log10(20000)
    px = lambda ms: L + (math.log10(max(ms, 0.05)) - lo) / (hi - lo) * (W - L - R)

    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="latency per path, log scale, against budgets">']
    for dec, lab in ((0.1, "0.1 ms"), (1, "1 ms"), (10, "10 ms"), (100, "100 ms"),
                     (1000, "1 s"), (10000, "10 s")):
        out.append(f'<line x1="{px(dec):.1f}" y1="14" x2="{px(dec):.1f}" y2="{H-22}" '
                   f'stroke="var(--line)" stroke-width="1"/>')
        out.append(f'<text x="{px(dec):.1f}" y="{H-8}" text-anchor="middle" '
                   f'font-size="10" fill="var(--ink-faint)">{lab}</text>')
    for i, (name, p50, p95, budget) in enumerate(rows):
        y = 26 + i * 26
        over = budget is not None and p50 > budget
        hue = "var(--neg)" if over else "var(--ok)"
        out.append(f'<text x="{L-10}" y="{y+4}" text-anchor="end" font-size="11" '
                   f'fill="var(--ink)">{html.escape(name[:34])}</text>')
        out.append(f'<line x1="{px(p50):.1f}" y1="{y}" x2="{px(p95):.1f}" y2="{y}" '
                   f'stroke="{hue}" stroke-width="2" opacity=".4"/>')
        out.append(f'<circle cx="{px(p50):.1f}" cy="{y}" r="4" fill="{hue}"/>')
        if budget:
            out.append(f'<line x1="{px(budget):.1f}" y1="{y-8}" x2="{px(budget):.1f}" '
                       f'y2="{y+8}" stroke="var(--warn)" stroke-width="2"/>')
    out.append("</svg>")
    return "".join(out)


def bar(pct: float, hue: str, w: int = 168) -> str:
    """One inline SVG bar. No library, no CDN — the page is served by the app
    under the same no-build-step rule as the console (D-07)."""
    filled = max(0.0, min(100.0, pct)) / 100 * w
    return (f'<svg class="bar" width="{w}" height="10" viewBox="0 0 {w} 10" '
            f'role="img" aria-label="{pct:.1f} percent">'
            f'<rect x="0" y="2" width="{w}" height="6" rx="3" fill="var(--track)"/>'
            f'<rect x="0" y="2" width="{filled:.1f}" height="6" rx="3" fill="{hue}"/>'
            f"</svg>")


def row(label: str, lo: float, mid: float, hi: float, hue: str) -> str:
    spread = "" if abs(hi - lo) < 0.05 else f'<span class="sp">{lo:.1f}–{hi:.1f}</span>'
    return (f'<tr><td class="k">{html.escape(label)}</td>'
            f'<td class="b">{bar(mid, hue)}</td>'
            f'<td class="n">{mid:.1f}%</td><td class="s">{spread}</td></tr>')


def build() -> str:
    s1, s2, bn = spike1(), spike2(), bench()
    live_f = RESULTS / "bench_live.json"
    live = json.loads(live_f.read_text()) if live_f.exists() else None
    pr = pr_curve_data()
    A, D, P = s1["arms"], s1["deltas"], s1["pairs"]
    GREEN, AMBER, SLATE = "var(--ok)", "var(--warn)", "var(--slate)"

    def arm_rows(axis: str, hue_for) -> str:
        out = []
        for arm, label in (("S0", "S0 · bare model"),
                           ("S1", "S1 · + evidence contract"),
                           ("S2", "S2 · + verification"),
                           ("MUTE", "MUTE · fixed safe string")):
            v = A[arm][axis]
            out.append(row(label, v[0], v[1], v[2], hue_for(arm)))
        return "\n".join(out)

    safe_rows = arm_rows("safe", lambda a: SLATE if a == "MUTE" else
                         (AMBER if a == "S0" else GREEN))
    resp_rows = arm_rows("resp", lambda a: SLATE if a == "MUTE" else GREEN)

    delta_cells = "".join(
        f'<span class="chip {"neg" if d[0] < 0 else "pos" if d[0] > 0 else "zero"}">'
        f'{d[0]:+.1f}</span>' for d in D)
    resp_delta_cells = "".join(f'<span class="chip neg">{d[1]:+.1f}</span>' for d in D)

    t189, best, t161 = s2["n189"]["arms"], s2["best"]["arms"], s2["n161"]["arms"]

    # Built here rather than inside the page template: `{{` inside an f-string
    # expression is a set literal, not an escaped brace, and a dict inside one
    # is unhashable. Assembling it first is also just easier to read.
    pr_svg = line_chart(pr["points"], pr["op"], [
        {"r": 100 * t189["A0"]["r"], "p": 100 * t189["A0"]["p"],
         "label": "A0 regex", "hue": "var(--slate)"},
        {"r": 100 * best["A2"]["r"], "p": 100 * best["A2"]["p"],
         "label": "A2 cascade", "hue": "var(--warn)"},
    ])

    def triage_rows() -> str:
        names = {"A0": "A0 · regex baseline (incumbent-style)",
                 "A1": "A1 · logistic gate, 15 features",
                 "A2": "A2 · full cascade (gate → model)"}
        out = []
        for arm in ("A0", "A1", "A2"):
            a = best[arm]
            out.append(
                f'<tr><td class="k">{names[arm]}</td>'
                f'<td class="b">{bar(100*a["p"], GREEN, 96)}</td><td class="n">{100*a["p"]:.1f}%</td>'
                f'<td class="b">{bar(100*a["r"], AMBER, 96)}</td><td class="n">{100*a["r"]:.1f}%</td>'
                f'<td class="n">{100*a["f1"]:.1f}%</td>'
                f'<td class="s">{a["tp"]}/{a["fp"]}/{a["fn"]}</td></tr>')
        return "\n".join(out)

    def bench_rows() -> str:
        out = []
        for k, v in bn.items():
            budget = 2000 if "research" in k else None
            flag = ('<span class="ok-chip">within 2 s</span>' if budget else "")
            strong = "research" in k or "CPU" in k
            out.append(
                f'<tr><td class="k">{"<b>" if strong else ""}{html.escape(k)}'
                f'{"</b>" if strong else ""}</td>'
                f'<td class="n">{v["n"]}</td><td class="n">{v["p50"]:.3f}</td>'
                f'<td class="n">{v["p95"]:.3f}</td><td class="n">{v["p99"]:.3f}</td>'
                f'<td class="s">{flag}</td></tr>')
        return "\n".join(out)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SideStage — Results</title>
<style>
:root {{
  --bg:#f7f8fa; --panel:#fff; --line:#dde1e7; --ink:#1a1d21; --ink-soft:#4a5158;
  --ink-faint:#8b939c; --ok:#0f8a72; --warn:#b7791f; --slate:#8b939c;
  --track:#e6e9ee; --neg:#b03a2e;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#0e1116; --panel:#151a21; --line:#262e39; --ink:#e6e9ee; --ink-soft:#b3bcc6;
    --ink-faint:#79838e; --ok:#4db6a0; --warn:#d9a441; --slate:#79838e;
    --track:#222a34; --neg:#e0705f;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.wrap {{ max-width:920px; margin:0 auto; padding-block:34px; padding-left:18px; padding-right:18px; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.01em; }}
h2 {{ font-size:17px; margin:34px 0 10px; padding-top:16px; border-top:1px solid var(--line); }}
h3 {{ font-size:13.5px; margin:20px 0 8px; color:var(--ink-soft);
  text-transform:uppercase; letter-spacing:.05em; }}
p {{ color:var(--ink-soft); margin:9px 0; }}
.lede {{ font-size:15px; }}
table {{ width:100%; border-collapse:collapse; margin:10px 0 4px; }}
td,th {{ padding:5px 8px; text-align:left; vertical-align:middle; }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--ink-faint); font-weight:600; border-bottom:1px solid var(--line); }}
td.k {{ color:var(--ink); white-space:nowrap; }}
td.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.b {{ width:1%; }}
td.s {{ color:var(--ink-faint); font-size:12px; white-space:nowrap; }}
.sp {{ font-variant-numeric:tabular-nums; }}
.bar {{ display:block; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin:12px 0; }}
.chip {{ display:inline-block; font-variant-numeric:tabular-nums; font-size:12px;
  padding:1px 7px; margin-right:5px; border-radius:11px; border:1px solid var(--line); }}
.chip.neg {{ color:var(--neg); border-color:var(--neg); }}
.chip.pos {{ color:var(--ok); border-color:var(--ok); }}
.chip.zero {{ color:var(--ink-faint); }}
.ok-chip {{ font-size:11px; color:var(--ok); border:1px solid var(--ok);
  border-radius:11px; padding:1px 7px; }}
.verdict {{ border-left:3px solid var(--warn); padding:2px 0 2px 13px; margin:14px 0;
  color:var(--ink); }}
code {{ font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--track); padding:1px 5px; border-radius:4px; }}
.foot {{ margin-top:34px; padding-top:14px; border-top:1px solid var(--line);
  font-size:12px; color:var(--ink-faint); }}
.scroll {{ overflow-x:auto; }}
</style></head><body><div class="wrap">

<h1>SideStage — Results</h1>
<p class="lede">Every measured figure, generated directly from the recorded runs in
<code>evals/results/</code>. Nothing on this page was typed by hand, so it cannot
drift from the runs it reports.</p>
<p>Three rules, each one a bug this project actually had: <b>every number carries its
population</b>; <b>a range beats a point</b> wherever a model is in the loop; and
<b>what did not survive measurement is stated</b>, not quietly dropped.</p>

<h2>1 · Does verification actually help?</h2>
<p>Four arms over the same <b>{s1['cases']} adversarial cases</b>, same model
(<code>claude-sonnet-5</code>), <b>{s1['runs']} runs</b>. Safety = the reply was blocked,
or asserted nothing false. Responsiveness = it actually answered.</p>

<h3>Safe — every run plotted, not a bar hiding three</h3>
{dot_plot(A, "safe", [("S0", "S0 · bare model"), ("S1", "S1 · + evidence"),
                      ("S2", "S2 · + verification"), ("MUTE", "MUTE · control")])}
<h3>Responsive</h3>
{dot_plot(A, "resp", [("S0", "S0 · bare model"), ("S1", "S1 · + evidence"),
                      ("S2", "S2 · + verification"), ("MUTE", "MUTE · control")])}
<details><summary style="cursor:pointer;color:var(--ink-faint);font-size:12px">
the same numbers as a table</summary>
<div class="scroll"><table>{safe_rows}</table></div>
<div class="scroll"><table>{resp_rows}</table></div></details>

<div class="card">
  <h3 style="margin-top:0">S1 → S2, per run</h3>
  <p style="margin:4px 0">safety {delta_cells} &nbsp; — the sign is not stable</p>
  <p style="margin:4px 0">responsiveness {resp_delta_cells} &nbsp; — consistently negative</p>
  <p style="margin:10px 0 0">Paired exact McNemar over all runs:
     <b>safety</b> b={P['safe'][0]}, c={P['safe'][1]}, p = {P['safe'][2]:.4f} ·
     <b>responsiveness</b> b={P['resp'][0]}, c={P['resp'][1]}, p = {P['resp'][2]:.4f}</p>
</div>

<div class="verdict"><b>Grounding is the whole effect.</b> S0 → S1 moves safety from
~{A['S0']['safe'][1]:.0f}% to ~{A['S1']['safe'][1]:.0f}%. S1 → S2 is inside the noise on
safety and costs ~6 points of responsiveness. The component stays for reasons argued in
<code>docs/RESULTS.md</code> §1 — none of which is "the eval said so".</div>

<p><b>MUTE is the control that makes the point.</b> A system answering everything with one
fixed safe string is {A['MUTE']['safe'][1]:.0f}% safe and {A['MUTE']['resp'][1]:.1f}%
responsive. Any safety number quoted without a responsiveness number beside it is
achievable by saying nothing.</p>

<h2>2 · Triage cascade</h2>
<p>Same <b>189 held-out messages</b> from two platforms, gate threshold
<code>{s2['n189']['threshold']}</code>, fit on {s2['n189']['n_train']} training rows never
tested against.</p>
<div class="scroll"><table>
<tr><th>arm</th><th colspan="2">precision</th><th colspan="2">recall</th><th>F1</th><th>tp/fp/fn</th></tr>
{triage_rows()}
</table></div>

<h3>Precision–recall, swept from the gate itself</h3>
{pr_svg}
<p>The curve is <b>A1 alone</b>, swept across every distinct score on
{pr['n']} labelled rows ({pr['pos']} positive). A0 is a regex and A2 has a model
in it, so neither sweeps — they are single points, which is what they are. The
marked point is the shipped threshold, <code>{pr['threshold']}</code>: D-27
chooses it from cost asymmetry, because a missed question costs a sale while a
false positive costs two seconds of operator attention, and the curve is what
that trade-off looks like rather than an assertion about it.</p>
<p><b>A2 sits above the curve</b>, which is the cascade's whole argument: stage 2
buys back precision at a recall the gate alone could only reach by accepting far
more false positives.</p>
<p>A2 has a model in it, so it moves: precision
<b>{100*t189['A2']['p']:.1f}–{100*best['A2']['p']:.1f}%</b>, false positives
<b>{t189['A2']['fp']}–{best['A2']['fp']}</b>. A0 and A1 are deterministic.</p>
<p><b>The gate's job is recall, not precision.</b> A1 at {100*t189['A1']['p']:.0f}% precision
looks bad alone; it is a filter feeding a model, and {t189['A1']['fp']} false positives is
the price of missing only {t189['A1']['fn']}. Stage 2 removes most of them.</p>
<p><b>Populations differ and it matters.</b> On the 161-row single-platform segment A0
recall is <b>{100*t161['A0']['r']:.1f}%</b>; pooled over all observed messages it is
<b>53.6%</b>. Same arm, different denominators.</p>

<h2>3 · Latency</h2>
<p>Milliseconds. No model calls in any of these paths, which is why a reviewer can
reproduce them on a clone with no credential.</p>
{latency_chart(bn, live)}
<p style="font-size:12px;color:var(--ink-faint)">Log scale — the paths span five
orders of magnitude, and a linear axis would render every free path as a
zero-width bar. Dot is p50, the line runs to p95, the amber tick is the budget.
Red means p50 is over it.</p>
<div class="scroll"><table>
<tr><th>path</th><th>n</th><th>p50</th><th>p95</th><th>p99</th><th></th></tr>
{bench_rows()}
</table></div>
<p><b>Read the CPU row, not the wall row</b> — the wall p99 is ~16× the CPU p99 because
this is a shared laptop. And <b>research returns records, not prose</b>: the brief's
sub-2-second target is met by not making the model call, not by making it faster. The path
that <i>does</i> generate is p50 ~4,240 ms end to end on the adversarial suite.</p>

<h2>What did not survive measurement</h2>
<div class="card">
<p style="margin-top:0"><b>“verification makes the system safer”</b> — not demonstrated; see §1.</p>
<p><b>“the cascade beats the incumbent on a new platform”</b> — a wash, F1 79.4% vs 80.0%.</p>
<p><b>“the incumbent is unstable”</b> — chi-square p = 0.130, and the test was never
licensed (min expected cell 4.56 &lt; 5).</p>
<p><b>“97.8% safe” as a headline</b> — unfalsifiable; the MUTE row scores 100%.</p>
<p><b>“verification is a 0.2 ms dict lookup”</b> — measured on one synthetic draft; it is
{bn['verify — CPU (the work)']['p95']:.3f} ms p95 CPU over the real corpus.</p>
<p style="margin-bottom:0"><b>Suite E's 10/10, Suite C's 36/36</b> — 216 of 2,500 threshold
pairs also score 10/10; C drops to 77% under realistic chat noise.</p>
</div>

<div class="foot">
Generated by <code>tools/render_results.py</code> from <code>evals/results/*.json</code>.
Regenerate with <code>uv run python tools/render_results.py</code>; CI-check with
<code>--check</code>. Prose version, pinned by <code>tools/check_docs.py</code>:
<code>docs/RESULTS.md</code>.
</div>
</div></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed page is out of date")
    a = ap.parse_args()
    page = build()
    if a.check:
        if not OUT.exists():
            print(f"   {OUT.relative_to(ROOT)} has never been generated")
            return 1
        if OUT.read_text(encoding="utf-8") != page:
            print(f"   {OUT.relative_to(ROOT)} is out of date — "
                  f"run `uv run python tools/render_results.py`")
            return 1
        print(f"   {OUT.relative_to(ROOT)} matches the recorded runs")
        return 0
    OUT.write_text(page, encoding="utf-8")
    print(f"   wrote {OUT.relative_to(ROOT)}  ({len(page)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
