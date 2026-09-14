"""Suite D — the golden tape. Deterministic, free, and it needs no credential.

Everything here runs against recorded fixtures through `ReplayClient(strict=True)`,
which **raises on a miss rather than substituting a safe refusal**. That choice is
the whole point of the suite:

  - a drifted system prompt changes the fixture key, so the tape fails loudly
    instead of quietly serving "let me check that and come back to you";
  - a `FixtureMissing` here is a real signal — either the contract moved and the
    fixtures need re-recording, or someone changed a prompt without meaning to.

The non-strict client is what production degradation uses (D-32). Strict is what
a test wants. Same class, one flag, and the difference is exactly the difference
between "keep the console working" and "tell me something changed".

Re-record with:  uv run python -m evals.record_fixtures
"""

from __future__ import annotations

import pytest

from app.catalog import get_catalog
from app.entities import get_resolver
from app.llm import FixtureMissing, ReplayClient
from app.models import Intent, Verdict
from app.pipeline import draft_reply
from app.session import Session


@pytest.fixture(scope="module")
def replay():
    return ReplayClient(strict=True)


@pytest.fixture(scope="module")
def cat():
    return get_catalog()


# --- the tape --------------------------------------------------------------
#
# Asserted on OUTCOME rather than wording. The exact sentence is the model's and
# will differ on a re-record; what must not drift is the verdict, whether claims
# were made, and whether each claim cites a fact that exists in the evidence.

TAPE = [
    ("is that 1st edition?",              Intent.ATTRIBUTE_Q,       "lot_001"),
    ("what set is that charizard from",   Intent.ATTRIBUTE_Q,       "lot_006"),
    ("is that zard graded",               Intent.GRADE_CONDITION_Q, "lot_006"),
    ("hows the centering on that zard",   Intent.GRADE_CONDITION_Q, "lot_006"),
    ("how much is the mew in the shop",   Intent.PRICE_VALUE_Q,     "lot_s02"),
    ("do you ship to canada",             Intent.SHIPPING_RETURNS_Q, "lot_001"),
]


@pytest.mark.parametrize("msg,intent,lot_id", TAPE)
def test_tape_replays_without_a_credential(replay, cat, msg, intent, lot_id):
    """B-103. This used to assert `verdict in (PASS, REPAIRED, BLOCKED)` — every
    member of the enum, which is true of any value the field can hold. It
    proved the call returned without raising and nothing else, and that is how
    FATAL-3 survived: the tape includes "is that 1st edition?", the flagship
    demo case, and could not notice when it stopped blocking.

    What a golden tape is FOR is that a recorded generation replays to the same
    verdict. Anything weaker is a smoke test wearing a regression test's name.
    """
    r = draft_reply(msg, intent=intent, lot=cat.lots.get(lot_id), catalog=cat,
                    resolver=get_resolver(), client=replay)
    assert r.draft.text.strip(), "a replayed draft must have text"
    assert not r.draft.degraded, (
        f"{msg!r} fell through to the replay fallback — no model answered, so "
        f"a clean verdict here means nothing was asserted (B-98)")
    assert r.draft.verdict is EXPECTED[msg], (
        f"{msg!r} replayed to {r.draft.verdict.value}, tape says "
        f"{EXPECTED[msg].value}. Either the fixture was re-recorded against a "
        f"different generation, or a verifier change moved the verdict.")


# The verdict each taped case is recorded AT. A golden tape whose expectation is
# "any of the three" is not a golden tape (B-103).
EXPECTED: dict[str, Verdict] = {
    "is that 1st edition?":            Verdict.PASS,
    "what set is that charizard from": Verdict.PASS,
    "is that zard graded":             Verdict.PASS,
    "hows the centering on that zard": Verdict.PASS,
    "how much is the mew in the shop": Verdict.PASS,
    "do you ship to canada":           Verdict.PASS,
}


def test_the_tape_contains_at_least_one_block(replay, cat):
    """B-103/B-99. The whole point of `app/verify.py` is that it fires, and a
    reviewer with no credential must be able to watch it happen. When every
    taped case passes, the safety component has no observable effect on the
    only path such a reviewer can walk — which is exactly what an adversarial
    review found: 0 of 18 recorded demo drafts blocked.

    This test fails loudly in that state rather than leaving it to be
    discovered. `MUST_BLOCK` in `evals/record_fixtures.py` is what keeps it
    green; if the recorder cannot get a blocking generation, this says so.
    """
    from evals.record_fixtures import MUST_BLOCK
    from app.llm import FixtureMissing
    from evals.run_guardrails import _INTENT_FOR, load

    blocked = []
    for row in load("guardrail_adversarial"):
        if row["chat_message"] not in MUST_BLOCK:
            continue
        try:
            r = draft_reply(row["chat_message"],
                            intent=_INTENT_FOR.get(row.get("violation_code", "none")),
                            lot=cat.lots.get(row.get("lot_id", "")), catalog=cat,
                            resolver=get_resolver(), client=replay)
        except FixtureMissing:
            continue
        if r.draft.verdict is Verdict.BLOCKED:
            blocked.append((row["chat_message"],
                            [v.code for v in r.draft.violations]))
    assert blocked, (
        "no recorded MUST_BLOCK case blocks on replay — a keyless reviewer "
        "cannot see the verifier fire at all. Re-record with "
        "`uv run python -m evals.record_fixtures --force-drafts`.")


@pytest.mark.parametrize("msg,intent,lot_id", TAPE)
def test_every_claim_cites_a_fact_that_exists(replay, cat, msg, intent, lot_id):
    """The contract, checked on recorded output.

    A claim naming `f9` when evidence stops at `f4` is the failure this catches —
    and it is one the verifier reports as `mis_citation` rather than crashing, so
    without an assertion it would pass silently.
    """
    r = draft_reply(msg, intent=intent, lot=cat.lots.get(lot_id), catalog=cat,
                    resolver=get_resolver(), client=replay)
    ids = {f.id for f in r.evidence.facts}
    for c in r.draft.claims:
        assert c.source_fact_id in ids, (
            f"{msg!r}: claim cites {c.source_fact_id}, evidence has {sorted(ids)}")


def test_observational_question_never_asserts_condition(replay, cat):
    """D-12 on the recorded tape: centring on a raw card is not ours to state."""
    r = draft_reply("hows the centering on that zard", intent=Intent.GRADE_CONDITION_Q,
                    lot=cat.lots.get("lot_006"), catalog=cat,
                    resolver=get_resolver(), client=replay)
    from app.models import Authority, ClaimType
    obs = {f.id for f in r.evidence.facts if f.authority is Authority.OBSERVATIONAL}
    assert obs, "a raw lot must carry an observational fact"
    for c in r.draft.claims:
        if c.type is ClaimType.CENTERING:
            pytest.fail(f"asserted centering on a raw card: {c.value!r}")


def test_strict_replay_raises_on_an_unrecorded_prompt(replay, cat):
    """The property that makes this suite a regression surface.

    If a miss returned a safe refusal, a changed system prompt would show up as
    the model politely declining — indistinguishable from working software.
    """
    with pytest.raises(FixtureMissing):
        draft_reply("what is the airspeed velocity of an unladen swallow",
                    intent=Intent.UNKNOWN, lot=cat.lots.get("lot_001"),
                    catalog=cat, resolver=get_resolver(), client=replay)


def test_console_replay_runs_end_to_end_with_no_key(cat):
    """The reviewer path: clone, no credential, click replay, get a queue.

    Non-strict here on purpose — this mirrors what a reviewer actually runs, and
    production degradation is non-strict too (D-32).
    """
    sess = Session(client=ReplayClient(strict=False))
    import json
    from pathlib import Path
    rows = [json.loads(l) for l in
            (Path(__file__).parent.parent / "evals" / "data" / "triage_test.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows[:60]:
        if "_meta" not in r:
            sess.ingest(r["text"])
    assert sess.stats()["seen"] >= 55
    assert sess.queue(), "replaying a real transcript must surface something"
    card = sess.draft(sess.queue()[0].id)
    assert card is not None and card.verdict


# --- the gap B-26 closed, and the check that keeps it closed ---------------


def _console_cards_without_a_fixture() -> list[tuple[str, str, str, str]]:
    """Every card the console's replay can surface, and whether it has a fixture.

    Driven through the real `Session` for the same reason the recorder is
    (`evals/record_fixtures.record_console_drafts`): the fixture key is a hash
    of the evidence block, so the only honest way to ask "does this card have a
    fixture" is to produce the card the way the console produces it.
    """
    import json
    from pathlib import Path

    data = Path(__file__).parent.parent / "evals" / "data"
    out = []

    # B-129. The curated DEMO cases are reached the same way — by typing them —
    # so they need the same check. `record_drafts` records them with a
    # HAND-SPECIFIED intent and never calls `classify`, so triage missed, said
    # `unknown`, and the draft assembled under `unknown` hashed to a key no
    # fixture had. All 22 curated cases were unreachable from the product they
    # were curated for, while the recorder reported them all recorded.
    from evals.record_fixtures import DEMO

    for msg, _intent, lot_id in DEMO:
        client = ReplayClient(strict=False)
        sess = Session(client=client)
        if lot_id:
            sess.set_active_lot(lot_id)
        sess.ingest(msg)
        cards = [c for c in sess.cards.values() if c.text == msg]
        if not cards:
            continue          # the gate dropped it; the console cannot show it
        client.misses.clear()
        sess.draft(cards[0].id)
        if client.misses:
            out.append(("DEMO", str(cards[0].intent), msg, client.misses[0]))

    for name in ("triage_test", "triage_show2"):
        p = data / f"{name}.jsonl"
        if not p.exists():
            continue
        rows = [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line.strip()]
        client = ReplayClient(strict=False)
        sess = Session(client=client)
        for r in rows:
            if "_meta" not in r:
                sess.ingest(r["text"])
        for card in list(sess.cards.values()):
            client.misses.clear()
            sess.draft(card.id)
            if client.misses:
                out.append((name, card.intent.value if hasattr(card.intent, "value")
                            else str(card.intent), card.text, client.misses[0]))
    return out


def test_every_card_the_console_can_surface_has_a_fixture():
    """B-26 recurring. A fixture miss is INVISIBLE: `_safe_draft` returns a
    reply that asserts nothing, so it earns a clean verdict and reads as the
    system being careful rather than as missing data.

    That is why `test_console_replay_runs_end_to_end_with_no_key` above cannot
    catch this — it is non-strict by design, and a degraded card satisfies
    `card.verdict` just as well as a real one. B-26 recorded 22 of 22 and said
    so; the corpus then grew and coverage regressed to 25 of 31 with every test
    still green.

    So this asserts coverage directly, against the same code path the reviewer
    clicks. Re-record with: uv run python -m evals.record_fixtures
    """
    missing = _console_cards_without_a_fixture()
    assert not missing, (
        f"{len(missing)} card(s) the console surfaces have no fixture, and each "
        f"will silently serve the safe refusal:\n" +
        "\n".join(f"  {src}  {intent:16} {text[:48]!r}  key={key}"
                  for src, intent, text, key in missing))
