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
    r = draft_reply(msg, intent=intent, lot=cat.lots.get(lot_id), catalog=cat,
                    resolver=get_resolver(), client=replay)
    assert r.draft.verdict in (Verdict.PASS, Verdict.REPAIRED, Verdict.BLOCKED)
    assert r.draft.text.strip(), "a replayed draft must have text"


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
