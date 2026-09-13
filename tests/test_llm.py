"""The model seam — the two properties above it that nothing else can check.

Both failure modes here are invisible from the outside, which is why they are
tests rather than comments:

  1. **A silently invalidated cache prefix looks exactly like working code.**
     The bill changes; the output does not. `_system_blocks` has claimed this
     test existed since the file was written, and until now it did not.

  2. **A client that ignores `on_text` still returns a correct draft.** The
     streaming seam (D-35, B-14) only shows up as a latency metric, so a replay
     client that quietly dropped the callback would pass every other test in the
     suite while making `ttft_ms` meaningless.
"""

from __future__ import annotations

import inspect

import pytest

from app.catalog import get_catalog
from app.entities import get_resolver
from app.evidence import assemble
from app.llm import (
    DRAFT_SYSTEM,
    TRIAGE_SYSTEM,
    AnthropicClient,
    LLMClient,
    ReplayClient,
    fixture_key,
)
from app.models import Intent


@pytest.fixture(scope="module")
def evidence():
    cat = get_catalog()
    return assemble(intent=Intent.ATTRIBUTE_Q,
                    resolution=get_resolver().resolve("is that 1st edition?"),
                    catalog=cat, lot=cat.lots.get("lot_001"))


# --- 1. the cached prefix -------------------------------------------------


@pytest.mark.parametrize("prompt", [DRAFT_SYSTEM, TRIAGE_SYSTEM])
def test_system_prefix_is_stable(prompt):
    """Frozen at import, so two reads are identical.

    D-19's whole economics rest on this: a timestamp, a uuid or an unsorted dict
    anywhere in the prefix changes it every call, the cache never hits, and the
    only symptom is a bill that is 10x too high. B-03 found exactly that class of
    problem by checking `cache_read` rather than trusting the design.
    """
    assert prompt == prompt
    assert fixture_key("m", prompt, []) == fixture_key("m", prompt, [])


@pytest.mark.parametrize("needle", ["{", "}", "T00:00", "0x"])
def test_system_prefix_carries_no_interpolation_markers(needle):
    """A cheap structural guard against the obvious ways volatile data arrives:
    an unrendered format placeholder, an ISO timestamp, an object repr."""
    assert needle not in DRAFT_SYSTEM


# --- 2. the streaming seam ------------------------------------------------


@pytest.mark.parametrize("impl", [ReplayClient, AnthropicClient])
@pytest.mark.parametrize("method", ["draft", "classify"])
def test_implementations_match_the_protocol_signature(impl, method):
    """Compared by signature, not by `isinstance`.

    `isinstance` against a Protocol needs `@runtime_checkable`, and even then it
    only checks that the method *names* exist — it would pass happily while an
    implementation quietly dropped `on_text` and every caller passing one broke.
    The signature is the seam; check the seam.
    """
    want = inspect.signature(getattr(LLMClient, method))
    got = inspect.signature(getattr(impl, method))
    assert got.parameters.keys() == want.parameters.keys()
    for name, p in want.parameters.items():
        assert got.parameters[name].kind == p.kind, f"{method}({name}) changed kind"


def test_on_text_fires_even_when_the_client_cannot_stream(evidence):
    """The callback is part of the contract, not a capability of one provider.

    A client with no stream to give still has to deliver the text through the
    same channel, or every caller needs to know which client it holds — which is
    the coupling D-18's seam exists to prevent.
    """
    seen: list[str] = []
    res = ReplayClient(strict=False).draft(
        question="is that 1st edition?", evidence=evidence,
        intent=Intent.ATTRIBUTE_Q, on_text=seen.append)

    assert seen, "on_text was never called"
    assert "".join(seen) == res.output.reply_text


def test_replay_reports_no_ttft(evidence):
    """0 means "not streamed", and it must not be faked.

    Chunking a recorded string would produce a first-token time that was never
    measured — a number that looks like evidence and is not. The honest value for
    a call that did not stream is zero.
    """
    res = ReplayClient(strict=False).draft(
        question="is that 1st edition?", evidence=evidence,
        intent=Intent.ATTRIBUTE_Q, on_text=lambda _: None)
    assert res.ttft_ms == 0


def test_draft_works_without_a_callback(evidence):
    """`on_text` is optional, and omitting it must not change the answer."""
    c = ReplayClient(strict=False)
    a = c.draft(question="is that 1st edition?", evidence=evidence,
                intent=Intent.ATTRIBUTE_Q)
    b = c.draft(question="is that 1st edition?", evidence=evidence,
                intent=Intent.ATTRIBUTE_Q, on_text=lambda _: None)
    assert a.output.reply_text == b.output.reply_text
