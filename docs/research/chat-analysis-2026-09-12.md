# Chat analysis — `saw_tcg` / ScythersTCG, 2026-09-12

Frame-by-frame reconstruction of live chat from a Whatnot Pokémon singles auction.

| | |
|---|---|
| Source | 105 frames sampled at 1 per 15 s from a screen recording |
| Coverage analysed | frames 1–81 ≈ **20 minutes** |
| Method | crop to the chat panel, read each frame, dedupe against the previous frame's tail |
| Deduped messages | **178** |
| Concurrent viewers | 188–201 |

Consecutive frames overlap by 4–6 messages throughout, so the log is continuous rather than
sampled. Frames 3 and 4 are byte-identical — **zero messages in fifteen seconds.**

---

## 1. Velocity — a lower bound, not a measurement

```
178 messages ÷ 1,215 seconds = 0.15 msg/s ≈ 8.8 messages per minute
```

**This figure is censored by the method, and must be quoted as a lower bound.**

The chat panel shows ~10 messages. At 15-second sampling, the fastest rate this method can
*detect* is 10 messages per frame gap:

```
ceiling at 15 s sampling = 10 msg / 15 s = 0.67 msg/s
ceiling at 30 s sampling = 10 msg / 30 s = 0.33 msg/s
```

Anything above that scrolls past between frames and is recorded as 10. And even a complete
turnover cannot distinguish ten messages spread evenly across fifteen seconds from ten
arriving in one — **the sampling rate destroys burstiness entirely.**

**What the data does support.** Across the seven intervals read at true 15-second granularity
(frames 1→6, 47→49), new-message counts were **4, 4, 0, 2, 1, 4, 3** — mean 2.6 per 15 s, or
**0.17 msg/s** — and *every one of those intervals retained overlap with the previous frame*.
The method never saturated there, so for those stretches 0.17 msg/s is a real measurement
rather than a floor. Frames 3 and 4 were identical: zero messages in fifteen seconds.

**What it does not support.** Most of the log was read at 30-second gaps, where one interval
showed ~9 new messages against a ceiling of ~10 — close enough to saturation that 178 must be
treated as a floor for those stretches.

### What survives, and what doesn't

| Claim | Status |
|---|---|
| Mean velocity is two orders of magnitude below the 30 msg/s design assumption | **Holds.** Even the method's absolute ceiling (0.67 msg/s) is 45× below it. |
| There is no "deluge" in the mean | **Holds.** |
| The actionable share is ~17% of traffic | **Holds** — a ratio, unaffected by censoring, provided misses are class-uniform. |
| **No backpressure or drop policy is needed** | **Does NOT hold** — but not because bursts were found. See below. |

### On burstiness: we can claim nothing in either direction

No bursts appear in the sampled frames. **That is not evidence that none occurred.** A burst
above the method's ceiling looks identical to a quiet interval: the panel turns over, ten
messages are recorded, and whatever else arrived is gone. So "chat is not bursty" is exactly
as unsupportable from this data as "chat is bursty" — the instrument cannot distinguish them.

The only burstiness observation available is qualitative, from watching rather than counting:
this mid-velocity show did not *feel* notably bursty; the 3–10 second rapid-fire formats may
have been; and a luxury seller's pre-auction pitch drew what looked like a short cluster of
euphoric replies. Impressions, recorded as impressions.

**The engineering consequence is a cheap one.** Keep a bounded queue — it costs nothing and
absorbs a burst if one happens. Do *not* build an elaborate drop policy, priority-shedding
scheme or backpressure ladder, because the evidence that would justify that machinery does
not exist. D-15 should say so plainly: the drop policy exists as insurance, not as a response
to a measured peak.

### How to measure it properly

Re-extract a 60–90 second window around a lot close at **1-second intervals**. At that rate
consecutive frames overlap almost completely, so only the newest message needs tracking and
the count becomes exact. Lot closes are the right window because the burst is visible in the
log already — `Ggs` / `GGs` / reaction clusters arrive together.

Until that exists, the honest statement is: **mean velocity ≥ 0.15 msg/s and almost certainly
under 0.7; peak unmeasured.**

---

## 2. Composition

Classification is my judgement on the message text; treat the boundaries as approximate.

Counts below are from `evals/data/triage_test.jsonl` (161 labelled messages).

| Class | n | share |
|---|---|---|
| `social` — banter, hype, reactions | 66 | 41.0% |
| `cross_user` — viewers talking to each other | 39 | 24.2% |
| **`market_comment`** — viewers supplying comps and pop figures | **22** | **13.7%** |
| `request` — run X, show the back, go quicker, check comps | 10 | 6.2% |
| `system_event` — Unlocked Bronze ×7, Raiding ×1 | 8 | 5.0% |
| `avail_q` | 7 | 4.3% |
| `grade_q` · `price_q` · `attribute_q` · `negotiation` · `unknown` | 9 | 5.6% |

**Seller-directed total: 27 of 161 — 16.8%.** Roughly one actionable message every 45
seconds. Not a firehose: a needle-in-haystack problem at low volume, which makes precision
*harder* than it would be at high throughput.

The single largest non-social class is **`market_comment` at 13.7%** — viewers supplying the
exact data the seller never states. That is the demand signal, and it outnumbers every
category of question they ask.

---

## 3. The incumbent already does detection — measured

Whatnot highlights some messages in orange. Every highlighted message in the log ends in a
question mark, and no message without one is highlighted, so the heuristic is almost
certainly punctuation-based.

**Computed from the labelled datasets**, which record both the class and whether the platform
highlighted each message — so this is derived from files, not estimated by hand. Three
segments of the same show were transcribed: `triage_extra_batch0.jsonl` (before),
`triage_test.jsonl` (middle), `triage_extra_batch2.jsonl` (after).

| segment | msgs | seller-directed | highlighted | recall | precision | F1 |
|---|---:|---:|---:|---:|---:|---:|
| batch0 · before | 83 | 10 | 5 | 40.0% | 80.0% | 53.3% |
| batch1 · middle | 161 | 27 | 16 | 40.7% | 68.8% | 51.2% |
| batch2 · after | 241 | 32 | 25 | **68.8%** | **88.0%** | 77.2% |
| **POOLED** | **485** | **69** | **46** | **53.6%** | **80.4%** | **64.3%** |

> **Correction, and it matters.** Earlier drafts quoted **41% / 69%** as *the* baseline. That
> was one segment. Across 485 messages the incumbent scores **53.6% recall, 80.4% precision**,
> which is a materially harder bar. Quote the pooled figure, or quote the range — never the
> middle segment alone.
>
> **The variance is itself a finding, and it is underpowered.** Recall swings 40% → 41% → 69%
> between segments while precision stays in a 69–88% band. The detector never changed, so
> whatever moved is a property of the traffic — what fraction of that segment's requests
> happened to carry a question mark — rather than of the detector.
>
> **How much of the swing is real, tested rather than asserted** (two-proportion z-test on the
> highlighted counts):
>
> | comparison | recall | p | |
> |---|---|---:|---|
> | batch0 vs batch1 | 40.0% vs 40.7% | 0.967 | indistinguishable |
> | batch0 vs batch2 | 40.0% vs 68.8% | 0.102 | **underpowered** — batch0 has 10 positives |
> | batch1 vs batch2 | 40.7% vs 68.8% | **0.031** | **real** |
>
> So: one comparison clears p < 0.05 and the spread is **not purely sampling noise**. But with
> 10–32 positives per segment a single message moves batch0's recall by ten points, and the
> claim that all 29 points are real instability is **not supported by this data**. Quote the
> batch1/batch2 gap, which is tested; treat batch0 as directional only.
>
> **What it means for Spike 2.** A classifier that reads intent rather than punctuation should
> be *stable* across segments, so segment-wise variance is worth reporting alongside the mean —
> but the same power limit applies to our own numbers. Stability will be **weak evidence
> either way** on n this size, and Suite A should report per-segment intervals rather than
> point estimates so that is visible rather than implied.

### Two minimal pairs prove the signal is punctuation and nothing else

The corpus contains two natural experiments — the same person asking the same thing twice:

| caught | missed |
|---|---|
| `Back again plz ?? Sorry` | `Back again plz` |
| `What are these silver boarders out of ??` | `what set is that pikachu silver border from` |

Same user, same request, opposite detection outcome. Confirmed structurally too: across all 485
messages, **every** highlighted row contains `?` and **no** unhighlighted row does.

### A small inter-rater check

Batch 0's final frames overlap batch 1's first frame by eight messages, labelled independently.
**All eight labels matched exactly.** Not a substitute for a proper agreement study, but it is
better than the "no inter-rater check" this analysis previously had to concede.

### What it misses (no question mark, high intent)

| Message | Why it matters |
|---|---|
| `no breaks!` · `lugia next!` | queue requests → `push_lot` |
| `Go quicker` | pace feedback to the seller |
| `320 for gare plz` | a named bid/offer |
| `what set is that pikachu silver border from` | genuine identity question |
| `Do u have the mew one` | catalog request |
| `You got any psyducks` | catalog request |
| `Any team rocket holos` | catalog request |
| `did i miss the skyridge` | closed-lot / late-arrival |
| `Pre bid Lugia so I can sleep` | **pre-bid request with a reason** |
| `is entering the givvy optional` | giveaway mechanics |

### What it falsely catches

`base set?` (one viewer asking another) · `Did it crash? It was $100 sealed last year`
(commentary, not a question) · `half off? we trollin` (banter) · `Yooo, atmosphere?? Song?`
(about the music) · `do you believe in grading pop control?` (opinion, off-topic).

**This is the competitive baseline to beat, and it is now a number rather than an
assertion.** It also reframes Spike 2: the task is not "detect questions" — a regex already
does that at **53.6% recall / 80.4% precision** pooled — it is detecting *intent without
interrogative syntax* and *filtering out chatter that happens to carry a question mark*.
Those are two different failures and the classifier has to fix both; raising recall by
loosening the threshold would make the precision worse, which is exactly why the operating
point has to be argued rather than picked.

**And the bar is not one number but two.** Because the incumbent's recall swings 40% → 41% →
69% across segments on a detector that never changed, beating the pooled mean is necessary
but not sufficient: a classifier that reads intent should also be **stable** where
punctuation is not. Suite A therefore reports per-segment scores alongside the pooled figure,
and treats the spread as a result in its own right.

### And detection is not the bottleneck anyway

Every one of the 11 correctly-highlighted messages was displayed in orange, in front of the
seller, and **none were answered.** Surfacing is solved and it doesn't help. The gap is the
answer.

---

## 4. The room is doing the seller's job — 15 instances

Viewers supplied market data continuously while the seller supplied none:

> `Unseen 2500` · `1.5 in a 7, 1.3 in a 6` · `8 is 3 k` · `Went for 1800 the other night` ·
> `That was 2500 raw damn gg` · `Psa10 77k my goodness` · `86k psa 10` · `1/3rd` ·
> `Better than english card` · `Half off` · `Wow cheap` · `This seems cheap` ·
> `Monster not last year` · `Steal` · **`Check comps`**

`Check comps` is a viewer telling the seller to do the thing this product does, in two words.

**They also self-serve on identification, and on correcting the seller:**

- `what set is that pikachu silver border from` → answered by *other viewers*:
  `It was the Japanese silver border` · `thats call of legends` · `Unseen`
- `base set?` → `Or fire red leaf` → `fire red` — resolved viewer-to-viewer
- `you almost said shining mewtwo` — a viewer catching the host mid-misidentification
- `tcg has it incorrectly classified as holofoil` — a viewer correcting a **catalog error**
- `Not the one in the hand` — a viewer disambiguating which card is being discussed
- `That front centering is to the left` — a viewer making a condition assessment from video

That last set is the strongest argument in the document. **Verification is already happening
in the room — manually, unreliably, and by unpaid volunteers.** The product does not have to
create demand for it; it has to make it authoritative and put it where the seller can use it.

---

## 5. Recurring, unanswered

| Ask | Instances | Answered |
|---|---|---|
| Back / condition of a card | **5** (`Back is clean ?`, `Back was clean yah?`, `is the dark dragonite clean ?`, `Back again plz ?? Sorry`, `So hard to find clean`) | none |
| `Any Blaziken?` | **2** (same user, minutes apart) | none |
| Catalog availability (`psyducks`, `team rocket holos`, `the mew one`, `bubble mew`, `dragonight`) | 5 | none |
| Closed-lot price / re-run (`gengar fire red`, `skyridge`, `lugia unseen force`) | 3 | none |

**The back of a card is the single most requested thing in the room and it is genuinely
unanswerable by the copilot** — back condition is observational (D-12). The response is a
*presentation prompt*, not an answer: **"5 people want to see the back."** This is also what
produced the seller's liability speech, so resolving it has a second payoff.

---

## 6. Entity references — every one breaks exact matching

`rakwaza` (Rayquaza) · `venasaur 151?` (Venusaur) · `dragonight` (Dragonite) ·
`gengar fire red` (word order) · `big boy gengar` (descriptor) · `bubble mew` (descriptor +
family) · `the mew one` (deixis) · `lugia unseen force` (set name singularised) ·
`skyridge` · `call of legends` · `primes` · `Glaceon lvx` · `team rocket holos` (set as
category) · `Japanese silver border` (description, no name at all) · `shining mewtwo` ·
`fire red leaf green blastoise` · `psyducks` (pluralised).

Seventeen distinct failure modes for exact lookup, from twenty minutes of one show.

---

## 7. Mechanics confirmed

- **Pre-bidding is real and used** — `Pre bid Lugia so I can sleep`. Buyers bid on lots
  before they go live, and time-zone pressure is a stated reason. Direct support for D-34.
- **Raids import audience** — `is raiding with a party of 32` moved 32 viewers in at once,
  ~16% of the room. A step change in audience mid-show is a signal worth having.
- **Giveaways drive the noise floor** — 48–54 entries at ~190 viewers is 25–28% of the room
  typing to enter. A meaningful share of the 58% social traffic is mechanical.
- **The bell chime is a red herring** — rang mid-sequence on a lot with 24 extensions and not
  at all on a lot with one. Abandoned.

---

## 8. What this changes

| | |
|---|---|
| **D-15** | The cascade's justification moves from load-shedding to precision at low volume. 0.15 msg/s needs no backpressure. Keep the interpretable scorer; drop the throughput framing. |
| **Spike 2** | Now has a measured incumbent to beat: **53.6% recall, 80.4% precision** pooled over 485 messages, from a punctuation heuristic. That is the baseline any classifier must exceed — and because the same heuristic scores 40%/41%/69% recall segment to segment, **stability across segments is part of the bar**. A far better empirical target than a synthetic PR curve. |
| **Spike 1** | Strengthened. Detection is solved and unhelpful; the answer is the gap. 15 instances of viewers supplying comps is the demand, stated. |
| **D-13** | `market_comment` is 8% of traffic and was not in the taxonomy. Add it. |
| **D-16** | Referents skew hard to catalog, closed lots and the shop — confirmed at larger n. |
| **PRD** | "Check comps" is the user request in the user's words. |

## 9. Limits of this analysis

- Classification is my judgement from message text alone; no inter-rater check.
- Frames 82–105 are not included. Themes were sampled and consistent, but the log stops at 81.
- The seller's audio is not in this analysis, so "unanswered" means *not answered in chat*.
  Several may have been answered aloud — though across the eight logged lots the seller was
  observed to state no verifiable fact about any card.
- One show, one seller, one evening.
