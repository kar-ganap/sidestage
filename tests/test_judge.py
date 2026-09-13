"""B-13's closure — the check no per-claim rule can perform.

Two properties, and the second is the one B-24 taught. The judge must catch a
reply that is true and beside the point; and it must stay quiet on correct
refusals, clarifying questions and terse answers, because a judge that flags
those would re-create the failure the verifier just had — a system refusing good
work and calling it safety.

These use a stub rather than the model: what is under test is the wiring
(advisory not blocking, never holds up a draft, degrades open), not the model's
judgement. The model's judgement is measured in B-32 against 12 labelled pairs.
"""

from __future__ import annotations

import time

import pytest

import app.judge as J
from app.judge import JudgeRunner, Opinion


def _no_key(monkeypatch):
    """`Settings` is frozen — swap the whole object, not a field on it."""
    import dataclasses
    monkeypatch.setattr(J, "settings",
                        dataclasses.replace(J.settings, anthropic_api_key=None))


def _with_key(monkeypatch):
    import dataclasses
    monkeypatch.setattr(J, "settings",
                        dataclasses.replace(J.settings, anthropic_api_key="sk-test"))


def test_an_unavailable_judge_does_not_block(monkeypatch):
    """Advisory means advisory. If the second opinion cannot run, the operator
    gets the draft and no warning — exactly as before this existed."""
    _no_key(monkeypatch)
    op = J.judge("is the centering good", "It's the Base Set Charizard.")
    assert op.responsive is True and op.errored is True
    assert op.warns is False, "an errored judge must never warn"


def test_an_empty_reply_is_not_judged():
    op = J.judge("anything", "   ")
    assert op.responsive and op.latency_ms == 0


def test_a_model_failure_degrades_open(monkeypatch):
    _with_key(monkeypatch)
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "anthropic":
            raise RuntimeError("upstream down")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    op = J.judge("q", "a")
    assert op.responsive is True and op.errored is True


def test_runner_never_blocks_the_caller():
    """`result()` returns None while pending rather than waiting. A state read
    must not sit on a model call."""
    r = JudgeRunner()
    r._pending["c1"] = r._pool.submit(lambda: (time.sleep(1.5), Opinion(True, "", 0))[1])
    t0 = time.perf_counter()
    out = r.result("c1", timeout=0.05)
    assert out is None
    assert time.perf_counter() - t0 < 0.5, "result() blocked the caller"


def test_unknown_card_returns_none():
    assert JudgeRunner().result("nope") is None


def test_warns_only_when_it_actually_objected():
    assert Opinion(False, "beside the point", 900).warns is True
    assert Opinion(True, "answers it", 900).warns is False
    assert Opinion(False, "judge unavailable", 0, errored=True).warns is False
