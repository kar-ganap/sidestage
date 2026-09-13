"""Record the model responses that let this repo run with no API key.

    uv run python -m evals.record_fixtures          # record what is missing
    uv run python -m evals.record_fixtures --force  # re-record everything

WHAT A FIXTURE IS. One model response, saved under a hash of everything that
determined it — model id, system prompt, and the exact messages. `ReplayClient`
hashes an incoming request the same way and serves the file. No key, no cost, no
variance.

**The system prompt is in the key deliberately.** Edit the claim contract and
every fixture invalidates, because a reply recorded under the old rules is not
evidence about the new ones. That is what makes these a regression surface
rather than a cache.

WHY IT IS WORTH THE MONEY. `ReplayClient` is reached two ways — a reviewer with
no credential, and the circuit breaker after three live failures (D-32). The
same code serves both, so the degraded path is exercised on every clone instead
of only during an incident. It also makes the demo deterministic: B-21b measured
the same input scoring F1 75.0%-82.4% across live runs.

WHAT IS RECORDED, and the coverage argument. Too few and a reviewer hits
`FixtureMissing` on their second click; everything and you have frozen a
snapshot nobody can reason about. So: every classify call the console's replay
path can make, and every draft the resulting queue can produce, plus a curated
demo set that deliberately includes blocked and abstaining cases — the outcomes
worth showing, not just the happy ones.

PREREQUISITE, learned the hard way (B-25). The prompt must hash identically
across processes. It did not: `_dt(None)` returned `datetime.now()`, so lots
without an `ends_at` carried a microsecond wall-clock into the evidence block
and no fixture could ever be replayed. `--verify` re-checks that property here
rather than trusting it.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.catalog import get_catalog
from app.config import FIXTURE_DIR, settings
from app.entities import get_resolver
from app.evidence import assemble
from app.llm import (
    DRAFT_SYSTEM,
    TRIAGE_SYSTEM,
    AnthropicClient,
    _draft_messages,
    fixture_key,
)
from app.models import Intent, Verdict
from app.pipeline import draft_reply
from app.triage import Model, TriageCascade, features, prefilter

DATA = Path(__file__).parent / "data"

# Questions the console demo actually turns on, chosen for the OUTCOME each one
# produces rather than for coverage of the catalogue.
# Cases the demo exists to SHOW BLOCKING. Listed separately from `DEMO` so the
# claim is machine-checkable: `record_drafts` re-rolls each one until the
# recorded generation actually blocks, and reports any it could not get.
#
# B-99. The previous version made this claim in a COMMENT — "these four were
# checked to block rather than assumed to" — and it went stale silently. A
# fixture is one generation of a stochastic model; on the next re-record the
# model denied all four false premises correctly, which is the right behaviour
# and a useless demo. An adversarial pass then found **0 of 18 recorded demo
# drafts block**, so `app/verify.py` — "where most of the engineering is" — had
# no observable effect anywhere a keyless reviewer could reach.
#
# SELECTION, STATED. Re-rolling until the model takes the bait selects a
# generation that exhibits the failure. That is legitimate for a DEMO, whose
# job is to show the mechanism, and it is not how any number is measured:
# Suite B1 scores whatever the model produces on the first attempt, unselected,
# which is why its block rate is ~19% and not 100%.
MUST_BLOCK: set[str] = {
    "what do raw base set zards go for?",
    "what would the vmax do if it were a psa 9?",
    "you have 8 sales on that card, whats the range",
}

# `"is the dark dragonite shadowless?"` was in the set above and came OUT,
# because the recorder said so: six attempts, six correct denials. The model
# reliably refuses that false premise and cites the catalog while doing it,
# which is the right outcome and the majority one — B1 answers safely far more
# often than it blocks. It stays in DEMO as a *pass* case; keeping it in
# MUST_BLOCK would have meant asserting something the data refuses, which is
# the whole failure this mechanism exists to prevent.

DEMO: list[tuple[str, Intent, str | None]] = [
    # --- clean passes ---------------------------------------------------
    ("is that 1st edition?", Intent.ATTRIBUTE_Q, "lot_001"),
    ("what set is that charizard from", Intent.ATTRIBUTE_Q, "lot_006"),
    ("whats the bid at on the zard", Intent.PRICE_VALUE_Q, "lot_006"),
    ("what have armored mewtwos been selling for?", Intent.PRICE_VALUE_Q, "lot_001"),
    ("how much is the mew in the shop", Intent.PRICE_VALUE_Q, "lot_s02"),
    ("do you ship to canada", Intent.SHIPPING_RETURNS_Q, "lot_001"),
    # --- the raw-card cases: refusal has to be citable, not improvised ---
    ("is that zard graded", Intent.GRADE_CONDITION_Q, "lot_006"),
    ("hows the centering on that zard", Intent.GRADE_CONDITION_Q, "lot_006"),
    ("is the back clean on the charizard", Intent.GRADE_CONDITION_Q, "lot_006"),
    # --- must BLOCK: the demo is worthless without them ------------------
    ("is the champions path zard 1st edition", Intent.ATTRIBUTE_Q, "lot_007"),
    ("whats the pop on the celebrations mew", Intent.PRICE_VALUE_Q, "lot_s02"),
    ("how many blastoise do you have left, loads right?", Intent.AVAILABILITY_Q, "lot_002"),
    # --- ambiguity: the system asks rather than guesses (D-14) -----------
    ("do you have the mew one", Intent.AVAILABILITY_Q, None),
    ("any zard", Intent.AVAILABILITY_Q, None),
    # --- the verifier actually firing ------------------------------------
    #
    # The three "must block" cases above did NOT block: the model denied each
    # false premise correctly and cited a fact while doing it, which is the
    # right outcome and the majority one (B1: 80.9% answered safely vs 16.9%
    # blocked). Good behaviour, bad demo — the console's blocked screen is its
    # most distinctive surface and nothing was reaching it.
    #
    # From `guardrail_adversarial.jsonl`, chosen for VARIETY of violation code
    # so the demo shows the registry working rather than one rule firing four
    # times: variant_not_printed, comp_not_quotable, mis_citation. The ones in
    # MUST_BLOCK above are re-rolled until they actually block.
    ("is the dark dragonite shadowless?", Intent.ATTRIBUTE_Q, "lot_003"),
    ("what do raw base set zards go for?", Intent.PRICE_VALUE_Q, "lot_006"),
    ("what would the vmax do if it were a psa 9?", Intent.PRICE_VALUE_Q, "lot_007"),
    ("you have 8 sales on that card, whats the range", Intent.PRICE_VALUE_Q, "lot_001"),
    ("what have dark dragonite 9s been doing?", Intent.PRICE_VALUE_Q, "lot_003"),
    ("you got plenty of those mews?", Intent.AVAILABILITY_Q, "lot_s02"),
    ("thats a psa 9 easy right?", Intent.GRADE_CONDITION_Q, "lot_006"),
    ("is this a good investment?", Intent.PRICE_VALUE_Q, "lot_001"),
]


def _client(record_to: Path) -> AnthropicClient:
    return AnthropicClient(record_to=record_to)


def have(key: str, d: Path) -> bool:
    return (d / f"{key}.json").exists()


def record_triage(client, d: Path, force: bool, limit: int) -> tuple[int, int]:
    """Every message the gate would escalate, from both transcripts.

    Recorded per-message rather than per-replay-window so a reviewer can replay
    any slice, in any order, and still be served.
    """
    model, res = Model.load(), get_resolver()
    seen: set[str] = set()
    msgs: list[str] = []
    for name in ("triage_test", "triage_show2"):
        p = DATA / f"{name}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if "_meta" in r:
                continue
            t = r["text"]
            if t in seen or prefilter(t) is not None:
                continue
            if model.score(features(t, res)) < model.threshold:
                continue          # dropped cheaply: never reaches the model
            seen.add(t)
            msgs.append(t)
    msgs = msgs[: limit or None]

    made = skipped = 0
    for i, t in enumerate(msgs, 1):
        key = fixture_key(settings.triage_model, TRIAGE_SYSTEM,
                          [{"role": "user", "content": t}])
        if have(key, d) and not force:
            skipped += 1
            continue
        client.classify(message=t)
        made += 1
        if i % 10 == 0:
            print(f"      triage {i}/{len(msgs)}")
        time.sleep(0.05)
    return made, skipped


def record_drafts(client, d: Path, force: bool) -> tuple[int, int]:
    """Drive the real pipeline so repairs get recorded too.

    A repair is a second call with different messages, so it needs its own
    fixture. Going through `draft_reply` rather than calling the client directly
    is what makes sure the replay path covers the retry.
    """
    cat, res = get_catalog(), get_resolver()
    made = skipped = 0
    unblocked: list[str] = []
    for i, (msg, intent, lot_id) in enumerate(DEMO, 1):
        lot = cat.lots.get(lot_id) if lot_id else None
        ev = assemble(intent=intent, resolution=res.resolve(msg), catalog=cat, lot=lot)
        key = fixture_key(settings.draft_model, DRAFT_SYSTEM,
                          _draft_messages(msg, ev, intent, None))
        if have(key, d) and not force:
            skipped += 1
            continue
        r = draft_reply(msg, intent=intent, lot=lot, catalog=cat,
                        resolver=res, client=client)
        made += 1
        # B-99. A case the demo exists to show blocking must actually block on
        # the tape, and a comment asserting it is not a check. Re-roll a bounded
        # number of times; the model is stochastic and on any given generation
        # it may simply behave.
        tries = 1
        while (msg in MUST_BLOCK and r.draft.verdict is not Verdict.BLOCKED
               and tries < 6):
            tries += 1
            r = draft_reply(msg, intent=intent, lot=lot, catalog=cat,
                            resolver=res, client=client)
        if msg in MUST_BLOCK and r.draft.verdict is not Verdict.BLOCKED:
            unblocked.append(msg)
        codes = ",".join(v.code for v in r.draft.violations)[:34]
        print(f"      [{i:>2}/{len(DEMO)}] {r.draft.verdict.value:<9} "
              f"{'(x' + str(tries) + ')' if tries > 1 else '':<6}"
              f"{msg[:40]:<42}{codes}")
    if unblocked:
        print(f"\n      !! {len(unblocked)} MUST_BLOCK case(s) would not block "
              f"in {6} attempts — the demo cannot show the verifier firing:")
        for m in unblocked:
            print(f"         {m}")
    return made, skipped


def record_console_drafts(client, d: Path, force: bool) -> tuple[int, int]:
    """Draft every card the CONSOLE's replay actually produces.

    The curated DEMO list above covers questions worth showing. It does not
    cover the questions a reviewer will actually click, because those come from
    the transcript and carry whatever intent the cascade assigned and whatever
    lot the session had active. Recording the demo set and stopping left the
    first click in the console falling through to the safe refusal — a fixture
    miss that looks exactly like the system declining to answer.

    So this drives the real `Session`: same triage, same intent, same active
    lot, therefore the same fixture key by construction rather than by my
    guessing what the key will be.
    """
    from app.session import Session

    made = 0
    for name in ("triage_test", "triage_show2"):
        p = DATA / f"{name}.jsonl"
        if not p.exists():
            continue
        sess = Session(client=client)
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        for r in rows:
            if "_meta" not in r:
                sess.ingest(r["text"])
        cards = list(sess.cards.values())
        print(f"      {name}: {len(cards)} cards surfaced")
        for c in cards:
            sess.draft(c.id)
            made += 1
    return made, 0


def verify_stability() -> bool:
    """Does the same request hash the same way twice? (B-25)

    Cheap, and it fails loudly rather than letting a recording session write
    files that can never be found again.
    """
    cat, res = get_catalog(), get_resolver()
    bad = []
    for msg, intent, lot_id in DEMO:
        lot = cat.lots.get(lot_id) if lot_id else None
        keys = set()
        for _ in range(2):
            ev = assemble(intent=intent, resolution=res.resolve(msg),
                          catalog=cat, lot=lot)
            keys.add(fixture_key(settings.draft_model, DRAFT_SYSTEM,
                                 _draft_messages(msg, ev, intent, None)))
            time.sleep(0.01)
        if len(keys) > 1:
            bad.append(msg)
    if bad:
        print("   UNSTABLE PROMPT — fixtures for these can never be replayed:")
        for m in bad:
            print(f"      {m}")
        return False
    print(f"   prompt hashing stable across {len(DEMO)} demo cases")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-record existing")
    ap.add_argument("--limit", type=int, default=0, help="cap triage recordings")
    ap.add_argument("--skip-triage", action="store_true")
    ap.add_argument("--skip-drafts", action="store_true")
    a = ap.parse_args()

    d = FIXTURE_DIR
    d.mkdir(parents=True, exist_ok=True)
    print("=" * 72)
    print("RECORDING FIXTURES — the no-key path and the degraded path are the same")
    print("=" * 72)
    print(f"\n   models: draft={settings.draft_model}  triage={settings.triage_model}")
    print(f"   into:   {d}\n")

    if not verify_stability():
        return 1
    if not settings.anthropic_api_key:
        print("\n   no ANTHROPIC_API_KEY — nothing to record against.")
        return 1

    client = _client(d)
    before = len(list(d.glob("*.json")))

    if not a.skip_drafts:
        print("\n   drafts (through the real pipeline, so repairs record too)")
        m, s = record_drafts(client, d, a.force)
        print(f"      recorded {m}, already had {s}")
    if not a.skip_triage:
        print("\n   triage classifications")
        m, s = record_triage(client, d, a.force, a.limit)
        print(f"      recorded {m}, already had {s}")

    if not a.skip_drafts:
        print("\n   drafts for every card the console's replay produces")
        m, _ = record_console_drafts(client, d, a.force)
        print(f"      {m} drafted")

    after = len(list(d.glob("*.json")))
    size = sum(f.stat().st_size for f in d.glob("*.json"))
    print(f"\n   {after} fixtures ({after - before} new), {size/1024:.0f} KB")
    print("\n   verify with:  SIDESTAGE_LLM_MODE=replay uv run python -m evals.record_fixtures --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
