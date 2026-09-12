# Live Show Observation Worksheet

**Purpose.** Watching a real card seller work. Not general impressions — specific counts
that confirm or kill assumptions already baked into `DECISIONS.md`. Every section says which
decision it tests. Live tally tool: the **Card Show Field Notes** artifact.

## How to run this

**Record; don't try to do it live.** Screen-record 45–60 minutes (Cmd+Shift+5 on macOS,
include system audio). Then two passes: **watch once with no notes** — you've never seen a
card show, spend the first fifteen minutes just absorbing it — then **scrub with the
worksheet open**, pausing between messages to tally and reading lot dwell and time-to-answer
straight off the scrubber. Nothing has to happen in real time. The recording is also Part 4,
the seed corpus for the tape simulator, so the thing that removes the pressure is something
you needed anyway.

**Catch the middle of a show.** The first 10–15 minutes is audience-building — greetings
dominate and sellers run filler lots to a small crowd, so the mix is unrepresentative. The
last 10–15 minutes skews to logistics ("can you combine my wins"). You're in the middle when
viewer count is flat or gently declining rather than climbing, and the host is running
material they're visibly excited about. Prefer a 1–2 hour show; a four-hour one almost
certainly has a mod or a team, which is different from our persona — note the duration
either way, it's data.

**The core mechanic.** A **tally window** is 30 *consecutive messages*, not 30 seconds.
Consecutive and unskipped, or you undercount `hype_noise` — the number you're trying to
measure. Message-bounded rather than time-bounded because a busy stream gives you 200 in a
minute (uncountable) and a quiet one gives you 5 (useless); fixing the count holds effort
and sample size constant and frees *elapsed time* to become the velocity measurement.

**Precision is not on the table, so don't aim for it.** n=30 is roughly ±18 percentage
points at 95% confidence. The instrument cannot tell 12% from 19% — it tells *a handful*
from *loads*, and every decision it feeds flips on a difference that large. A miscounted
message changes nothing.

## Priority — the sheet is over-built for one pass

| | |
|---|---|
| **Tier 1** — do this or the exercise failed | The recording · one tally window · the `unknown` verbatims |
| **Tier 2** — nearly free once you're scrubbing | Three lot dwell times · miss rate (of 10 questions, how many answered) |
| **Tier 3** — skip without guilt | Everything else. The recording means you can always go back. |

Tier 1 + Tier 2 is about fifteen minutes of scrubbing.

---

## Part 0 · Setup — 1 minute

| | |
|---|---|
| Platform | eBay Live / Whatnot / other: ______________ |
| Seller / show | ______________________________ |
| Date + time (your local) | ______________________ |
| Concurrent viewers at join | __________ (target 100–1,000) |
| Solo operator? | Yes / No / Can't tell — if No, is a mod answering in chat? ______ |
| Format | BIN drops / auction lots / break / mixed |
| Category | Pokémon / sports / other: ____________ |

> If viewers are under 50 or over 5,000, or it's a break, find a different show. Wrong
> band = wrong conclusions.

---

## Part 1 · Tally Window A — 5 minutes

Wait for chat to be moving. Then tally **30 consecutive messages**, in order, no skipping.

**Start time ______ : ______ → End time ______ : ______**
**Elapsed for 30 messages: ________ seconds → velocity = 30 ÷ elapsed = ______ msgs/sec**

| Class | What it looks like | Tally | n |
|---|---|---|---|
| `hype_noise` | 🔥🔥, "gm", "let's go", "first" | | |
| `attribute_q` | "is that 1st ed?", "shadowless?", "reverse holo?" | | |
| `grade_condition_q` | "centering?", "whitening?", "cert #?" | | |
| `price_value_q` | "what's it going for?", "last sold?" | | |
| `availability_q` | "how many left?", "any more?" | | |
| `shipping_returns_q` | "combined shipping?", "when does it ship?" | | |
| `authenticity_q` | "is it authenticated?", "vault?" | | |
| `buy_commit` | "I'll take it", "sold", "mine" | | |
| `negotiation` | "$350 shipped?", "best offer?" | | |
| `off_topic_abuse` | | | |
| **`unknown`** | **fits none of the above — write the text below** | | |
| | | **TOTAL** | **30** |

**Every `unknown` message, verbatim** (this is the most valuable data on the page —
it's the tail-coverage metric from D-13):

1. ________________________________________________
2. ________________________________________________
3. ________________________________________________

> **Tests D-13.** Assumed: `hype_noise` 50–60%, `unknown` 10–15% of non-noise. If `unknown`
> is above ~25%, the taxonomy is wrong and we change it *before* building the classifier.

---

## Part 2 · Free observation — 10 minutes

Stop counting. Watch. Answer these.

**Lot cycle** — time three consecutive lots from "held up to camera" to "next lot":

Lot 1: ______ min · Lot 2: ______ min · Lot 3: ______ min

> **Tests D-19.** The prompt-cache breakpoint 2 assumes a pinned lot is stable for 2–10
> minutes. If lots turn over in 40 seconds, that cache layer is worthless and the design
> changes.

**Missed questions.** Pick a moment when chat is busy. Watch 10 questions go by. How many
did the host answer, in any form?

Answered: ______ / 10 → **miss rate ______%**

> **The core value prop.** If the host answers 9 of 10, the product is much weaker than
> assumed and the PRD needs to say so honestly.

**Time to answer.** For three questions that *did* get answered, how long from message to
host's reply?

______ s · ______ s · ______ s

> **Tests the latency budget.** We committed to p95 ≤ 1500ms for a draft. If a human takes
> 15 seconds and nobody minds, sub-2s may be over-engineering — which is worth knowing and
> worth saying out loud rather than defending a number nobody needs.

**Repeat questions.** Roughly how many times did you see essentially the same question
asked by different people during these 10 minutes? ______

Most-repeated question, verbatim: ________________________________________

> **Tests the clustering value prop.** Near-duplicate collapse is half the queue's value.

**Unverifiable claims by the host.** Did the seller say anything they could not have known
for certain — stock they didn't check, a comp from memory, a condition call, a shipping
promise? Note each:

- ________________________________________________
- ________________________________________________

> **This is the guardrail thesis in the wild.** Even one is a PRD quote. Zero is also a
> finding, and an uncomfortable one worth recording.

**Actions the host takes mid-show.** What do they actually *do* besides talk? (change a
price, pin something, pull up a listing, check stock, look something up, mention running
low...)

- ________________________________________________
- ________________________________________________
- ________________________________________________

> **Tests D-04.** We chose push / swap / markdown / stock-adjust. If the host spends their
> time on something else entirely, we picked the wrong four.

---

## Part 3 · Tally Window B — 5 minutes

Same exercise as Part 1, ten minutes later. This catches whether the mix shifts with show
phase (openers vs. the good lots vs. the wind-down).

**Elapsed for 30 messages: ________ s → velocity ______ msgs/sec**

| Class | n | | Class | n |
|---|---|---|---|---|
| `hype_noise` | | | `authenticity_q` | |
| `attribute_q` | | | `buy_commit` | |
| `grade_condition_q` | | | `negotiation` | |
| `price_value_q` | | | `off_topic_abuse` | |
| `availability_q` | | | **`unknown`** | |
| `shipping_returns_q` | | | **TOTAL** | **30** |

New `unknown` messages, verbatim:

1. ________________________________________________
2. ________________________________________________

---

## Part 4 · Raw capture — do this, it's the highest-value artifact

Screenshot or screen-record **2–3 minutes of the chat column.** This becomes seed material
for the tape simulator, and it is the one thing that upgrades the eval from "synthetic" to
"phrasing sampled from observed live chat" — which directly addresses the biggest stated
limitation in D-29.

- [ ] Captured — saved to: ______________________________

> **Anonymize before anything enters the repo.** Replace real usernames with generated
> handles. We're sampling phrasing and rhythm, not people.

---

## Part 5 · Sanity check — 2 × 5 minutes, no counting

Two other sellers. One question each: **is this recognizably the same rhythm?**

**Stream B** ____________________ · viewers ________
Same / different, and how: ________________________________________________

**Stream C** ____________________ · viewers ________
Same / different, and how: ________________________________________________

---

## Part 6 · Verdict — fill this in immediately, before you forget

**Confirmed** (assumptions that held):

- ________________________________________________
- ________________________________________________

**Killed or dented** (assumptions that did not survive — these are the valuable ones):

- ________________________________________________
- ________________________________________________

**Surprised me** (anything I didn't have a category for):

- ________________________________________________
- ________________________________________________

**One thing this changes in the build:**

________________________________________________________________

---

## Scorecard — copy these into the PRD

| Metric | Assumed | Observed |
|---|---|---|
| Chat velocity (msgs/sec) | ~0.5–5 | |
| `hype_noise` share | 50–60% | |
| `unknown` share of non-noise | 10–15% | |
| Question miss rate | high (untested) | |
| Human time-to-answer | untested | |
| Lot dwell time | 2–10 min | |
| Unverifiable host claims per 10 min | ≥1 (untested) | |

Anything in the Observed column is real evidence and goes in the PRD by number. Anything
still blank stays explicitly listed as unvalidated, with a first-week discovery plan —
which the challenge asks for by name.
