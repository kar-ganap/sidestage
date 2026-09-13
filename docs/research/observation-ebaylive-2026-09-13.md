# Field observation 2 — eBay Live, a different seller on a different platform

**18.2 minutes** · 218 frames sampled at 5 s · `~/Desktop/ebaylive`
Extracted by `tools/extract_frames.py` (chat, seller speech, lot state).

**Why this show.** The single largest weakness in the project was that the
training and test segments were the *same show and the same seller*, so every
number measured generalisation across time within one broadcast. This is a
different seller on a **different platform — eBay's own**, which is the
corporate partner's surface.

| | Whatnot (2026-09-12) | eBay Live (2026-09-13) |
|---|---|---|
| seller | saw_tcg / ScythersTCG | **Old Skool Pokemon** ("Nick") |
| feedback | — | 99.9% positive · 61,289 · member since Jan 2016 |
| concurrent viewers | ~190–200 | **63** |
| stock | graded slabs | **mostly raw, $1 start** |
| winning bids | $111–$410 | **$1–$70** |
| lot timer | 8–20 s + extensions | 20 s + extensions |
| visible chat history | scrollable | **three messages** |
| seller speech | not captured | **live captions — captured** |

---

## 1. The three-message chat horizon is the most consequential thing here

eBay Live shows the operator **three chat messages at a time**. A fourth
arrives and the first is gone. The seller can scroll, but not while holding a
card to camera and talking.

**This is the strongest argument for the product that field work has produced,
and it was not available from the Whatnot data.** On Whatnot a missed question
is recoverable — scroll back and it is still there. Here the question is
*gone*, and the only way it survives is if something kept it. A triage queue
stops being a convenience that saves attention and becomes **the only durable
record of what the room asked**.

It also changes the operating-point argument in D-35/B-19. That derivation
capped the surfaced rate at what an operator can absorb, treating chat itself
as the fallback for anything triage drops. On this platform there is no
fallback: a dropped message is not deferred, it is destroyed. The asymmetry
between a missed buyer and a wasted glance is **larger here**, which argues the
threshold should be looser on eBay Live than the one fitted on Whatnot.

## 2. The seller reads chat aloud, verbatim — and now we can prove it

eBay Live carries live captions, so the seller's speech is transcribable. The
Whatnot observation explicitly could not do this and recorded "nothing factual
about any card" as an absence of evidence.

Matched pairs from the capture, chat against captions seconds later:

| a viewer typed | the seller then said |
|---|---|
| `Dang yeah I joined a bit late no worries k!` | *"Dang yeah, I joined a bit to… No Worries"* |
| `why are ghost types so heavy` | *"Some lengths and weights are wild for Pokemon with…"* |
| `With that tail it's like a chipmunk in body` | *"that hail, it's like a chipmunk of…"* |
| `Isn't it a theory ash is wicked strong with some of the Pokémon he picks up` | *"Aha, isn't it any Theory Wicked Strong with some Pokemon he picks up"* |

**He is reading messages out and answering them in the same breath.** Two
consequences:

- The seller's attention is genuinely on chat, so a queue that surfaces the
  right three things substitutes directly for the scanning he is already doing.
  This is not a workflow we would be introducing.
- **Answers are spoken, not typed.** Nothing in the chat log records that a
  question was answered. Any "unanswered question" metric built from chat text
  alone would be wrong on this platform, and the honest version needs the
  caption track.

## 3. Ungraded stock makes D-12 load-bearing rather than defensive

The catalogue is raw singles at $1 start, not slabs. `grade_condition_q` on
this show cannot be answered from a cert number, because there is no cert —
condition is **observational**, which D-12 says is never assertable.

So on eBay Live a much larger share of condition questions must route to *"the
seller has to answer this, I cannot"*. The observational-authority rule stops
being a safety edge case and becomes the common path. Worth testing
deliberately: the current catalogue is slab-heavy and under-exercises it.

Two smaller consequences. The **$250 authenticity value gate** almost never
fires at these prices. And **comp matching loses its grade key** — `set:number:GRADE`
is structural in `catalog.comp_key`, and raw comps have no grade to match on.

## 4. Shipping is answered by the UI, which may explain the anomaly

A persistent banner carries **`$5.00 Flat`** shipping plus cancellation terms.

`shipping_returns_q` appears **zero times in 477 Whatnot messages** despite the
primer calling it "the highest-volume policy question". The standing hypothesis
was that the primer was wrong. A better one: **the platform already answers it
in a banner nobody has to ask about.** That is a different finding, and it
demotes `shipping_returns_q` in D-22's automation ladder for a reason — the
question is absent because it is pre-empted, not because buyers do not care.

Still a hypothesis; this show has no shipping questions either, which is
consistent with it but does not establish it.

## 5. Viewers address the seller by first name — a feature the model lacks

```
nick did you see that galade SAR the tourney promo
Nick you should led a campaign to reduce price of Charizard from overrated to rated
Night night Nick and night to everyone see everyone Sunday
```

The triage scorer has `at_mention` as a strong **negative** (−1.17), because on
Whatnot an `@handle` almost always meant one viewer answering another. Here the
seller is addressed by bare first name, and the model has no feature for it.

Worse, the third example shows the name is *not* sufficient on its own —
"Night night Nick" is a farewell to the room. So the feature is "names the
seller **and** asks something", not "names the seller".

**This is exactly the kind of gap a second show exists to find**, and it is one
the Whatnot corpus could not have surfaced.

## 6. Auction mechanics, extracted rather than eyeballed

Fourteen lots, `$1` start, from `lots_raw.tsv`:

| lot | duration | first seen | final | lot | duration | first seen | final |
|---|---:|---:|---:|---|---:|---:|---:|
| 258 | 75 s | $2 | $32 | 265 | 65 s | $1 | $69.55 |
| 259 | 55 s | $1 | $68 | 266 | 65 s | $1 | $42.77 |
| 260 | 165 s | $1 | $24 | 267 | 70 s | $1 | $32 |
| 261 | 115 s | $1 | $42 | 268 | 70 s | $2 | $30 |
| 262 | 55 s | $1 | $34 | 269 | 85 s | $1 | $18 |
| 263 | 35 s | $1 | **$1** | 270 | 40 s | $1 | $6 |
| 264 | 65 s | $5 | $34 | | | | |

**Median lot 65 s — roughly 4× the Whatnot rapid-fire show.** That matters for
D-16: the pace-aware referent prior was argued from 3–10 s lots where a question
about the active lot is impossible for anyone. At 65 s a viewer *can* type about
what is on screen, so the pinned-lot prior should carry real weight here. The
mechanism was designed to shift with velocity and this is the first data point
at the other end of the range.

Lot 263 is the `stalled` case Suite E has only one instance of: **$1 to $1 over
35 s**. A second one, from a different platform.

---

## 7. Limits of this observation

- **OCR, not transcription.** Chat and captions were read by tesseract from
  text composited over live video. `chat_messages.txt` is a *draft for a human
  to label*, not data. Roughly 45 of 126 extracted lines are clearly real
  messages; the rest is video bleed and must be skipped, not labelled.
- **The seller transcript is legible, not accurate.** The caption is a rolling
  translucent overlay, and the overlap merge is defeated often enough that the
  transcript repeats itself. Good enough to establish *that* he reads chat
  aloud; not good enough to quote him on a fact about a card.
- **Sampling censors the chat rate.** 5 s frames against a 3-message window
  means any message is lost when four or more arrive inside one window. Every
  rate computed from this is a **lower bound** — the same censoring the Whatnot
  velocity figure was corrected for.
- **No platform highlight to score against.** eBay Live has no visible
  equivalent of Whatnot's orange marking, so `highlighted` cannot be read off
  the UI. The incumbent comparison on this show has to *apply* the
  question-mark rule characterised on Whatnot rather than observe the
  platform's own answer. A weaker claim, and it should be stated as one.
- **n is one show, 18 minutes, 63 viewers.** A quiet weeknight stream on a
  smaller platform surface. Nothing here establishes a distribution.
