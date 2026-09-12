# Field observation — 2026-09-12 · `saw_tcg` / ScythersTCG

Lot-by-lot observation of one Whatnot Pokémon singles auction, from a ~45 minute
recording plus live watching. **Ten lots logged, $3,077 of sales.**

This is the primary evidence base for the PRD. Unlike
[`observation-2026-09-11.md`](observation-2026-09-11.md), which was cursory survey work,
these are timed readings off a recording with bid figures taken from the screen.

| | |
|---|---|
| Platform | Whatnot |
| Seller | `saw_tcg` / ScythersTCG, 4.9★ |
| Concurrent viewers | **188–200** |
| Format | auction, singles, both English and Japanese |
| Lot numbering observed | **#328** |
| Listing title convention | `a NO CANCEL Singles - ScythersTCG #328` |

**Lot #328 is the number that explains everything else in this document.** Every attention
finding below has the same cause: this is lot three hundred and twenty-eight.

---

## 1. The lot log

Timer mechanics: a base timer per lot (8–20 s observed). A bid landing under a threshold of
roughly 6–8 s resets the clock — **always to less than the original base** (5–8 s observed).

| # | Lot | base | ext | at 1st ext | final | delta | ext-phase share | $/late bid |
|---|---|---|---|---|---|---|---|---|
| 1 | Armored Mewtwo PSA 10 | 15s | 7 | $195 | $330 | +$135 | 41% | $19 |
| 2 | Dark Dragonite | 20s | 3 | $320 | $360 | +$40 | 11% | $13 |
| 3 | **Pikachu (FireRed/LeafGreen)** | 8s | **3** | $111 | $111 | **$0** | **0%** | — |
| 4 | **Flying Pikachu** | 8s | **24** | $27 | $350 | +$323 | **92%** | $13 |
| 5 | Special Delivery Pikachu | — | 6 | $252 | $360 | +$108 | 30% | $18 |
| 6 | Pikachu (restroom break) | 4 min | 1 | $510 | $510 | $0 | 0% | — · **16 bids** |
| 7 | Pikachu, Japanese (#328) | 15s | 7 | $135 | $200 | +$65 | 33% | $9 |
| 8 | **Surfing Pikachu, Japanese** | 13s | **15+** | $6 | $266 | +$260 | **98%** | $17 |
| 9 | Birthday Pikachu | — | 11 | $266 | $410 | +$144 | 35% | $13 |
| 10 | Shibuya Pikachu, Japanese (#332) | 15s | 2 | $165 | $180 | +$15 | 8% | $8 |

**Ten lots, $3,077.** Extension counts: 1, 2, 3, 3, 6, 7, 7, 11, 15+, 24 — a wide, strongly
right-skewed distribution with a clear outlier at the top. A threshold at five flags lots 1,
4, 5, 8 and 9 and leaves the rest alone.

Lot 9's bid count is visible on screen: **22 bids against 11 extensions — 50% of bids landed
late.** Compare lot 6: 16 bids, 1 extension, **6% late.** Similar demand, opposite endgames.
Extension count isolates the contested close exactly as intended.

### 1a. Extension count measures the *endgame*, not total demand

Lot 6 settles this: **16 bids, 1 extension.** Extensions fire only on bids landing under the
threshold, so fifteen arrived early and one arrived late. The "$9–19 per extension" figure
throughout this table is therefore **dollars per late bid**, not per bid.

That makes extension count *more* useful than a raw bid count for our purposes. A lot that
takes sixteen early bids and closes cleanly needs no help — the market found it. A lot still
drawing late bids after twenty-four resets is in a genuine contest with runway left.

### 1b. The stall rule

Two lots had zero delta, and they are not the same thing:

- **Lot 6** — one extension, no movement. *Normal.* The last bid lands, the clock extends,
  nobody tops it. Every auction ends this way.
- **Lot 3** — three extensions, no movement. *Anomalous.* Something extended it twice more
  with nothing happening, which points at a seller stretching toward a number.

> **Stall trigger: `extensions >= 2 AND delta == 0`.** Not `delta == 0` alone.

### 1c. The endgame accelerates by design

The reset is **always shorter than the base timer** — confirmed across every lot. So the
intervention window shrinks exactly as a lot becomes more valuable: ~8 s at extension 3,
~5 s at extension 15, with bids landing at 2–3 s.

**This derives the latency budget from the domain rather than from a round number:**

```
one reset window                ≈ 5.0 s
human reads it and starts speaking ≈ 3.0 s
──────────────────────────────────────────
system budget                    ≈ 1.5–2.0 s
```

Two design constraints follow. The nudge must be **glanceable, not readable** — one line the
seller absorbs at a glance and says aloud (`pop 412 · 38 higher`), never a sentence. And
**early detection is mechanically necessary**, not merely preferable: intervening at
extension 3 has three times the runway of extension 15.

---

## 2. What the seller actually said

Complete inventory across ten lots and $3,077 of sales.

| Lot | Seller speech |
|---|---|
| 1 Armored Mewtwo (+$135) | nothing |
| 2 Dark Dragonite | nothing |
| 3 Pikachu — stalled, "base set?" unanswered | nothing |
| 4 Flying Pikachu (+$323, 24 ext) | **small talk with one commenter, about something else** |
| 5 Special Delivery | **slang praise, before bidding opened** |
| 6 Pikachu — 4 min absence, 6 questions waiting | showed off an alcohol bottle, talked about getting drunk on Friday |
| 7 Japanese Pikachu | consignment explanation · "they are all prebid" |
| 8 Surfing Pikachu (+$260, 15+ ext) | **lightly talking the card up during the auction** |
| 9 Birthday Pikachu (+$144, 11 ext) | **"the prices are a steal for this card" — repeatedly** |
| 10 Shibuya Pikachu | **a joke: "you rarely have pokemon committing a crime, but in this one they are committing vandalism"** |
| between lots | *"all auction sales are final. if you didn't see the back of the card, that's not my problem"* · *"you have to bid responsibly"* |

**Not one verifiable fact was stated about any card.** Four lots got promotion; **all four
were evaluative** ("this one's fire", "it's a steal") rather than factual ("pop 412, 38
higher, last three sold $380–420"). The seller's longest and most deliberate speech was a
liability disclaimer.

Lot 9 is the sharpest instance. *"The prices are a steal for this card"*, said repeatedly, is
a value argument made with an adjective because the speaker has no numbers to hand. Silence
was ambiguous — it could have meant no need. **A value argument attempted without evidence is
not ambiguous.** He has the intent and lacks the ammunition, three times in one lot.

### 2a. The causal direction is ambiguous, and it matters

Lots 5 and 8 were promoted and both performed. But **lot 4 ran 24 extensions and 92%
extension-phase value with nothing said at all.**

So presentation is plainly not necessary for a hot lot, and the likelier causal direction is
the reverse: **the seller talks about lots that are already visibly hot.** Promotion is a
*response* to demand, not a driver of it.

That does not weaken the product — it sharpens the claim. If the seller notices heat at
extension ten and the system notices at extension three, **the value is the seven extensions
of runway in between.** Not *"tell them to talk"* but *"tell them earlier than they would
have noticed."* Measurable as time-to-detection vs time-to-seller-reaction.

*Open:* on lot 8, did the talking begin before or after the bidding heated up? That single
reading settles the direction.

### 2b. Silence is structural, not negligent

During a hot lot the seller is watching a five-second clock and a moving bid, at lot 328 of
the night. Talking competes with monitoring. This is a better framing for the PRD than
"sellers miss things," because structural problems are what tools fix.

---

## 3. Questions and requests, with referents

Everything seller-directed captured across the session:

| Message | Referent | Answerable? | Answered? |
|---|---|---|---|
| "Any blazikens?" **(asked twice)** | catalog | ✅ | ❌ |
| "is the back clean?" / "back was clean yeah?" | current lot | ❌ observational | ❌ |
| "Swirl?" | current lot | ❌ observational | ❌ |
| ~~"base set?"~~ | — | — | **Reclassified `cross_user`.** Frame analysis shows `mtheory44` tagged `@selladoncitygamecorner` about a Charizard *that viewer* owns. Never a question to the seller — and the platform highlighted it anyway. See [`chat-analysis-2026-09-12.md`](chat-analysis-2026-09-12.md). |
| "how much did gengar red fire go for?" | closed lot | ✅ | ❌ |
| "just came in what did pokemon delta species sell for?" | closed lot | ✅ | ❌ |
| "is the dark dragonite clean?" | closed lot | ❌ observational | ❌ |
| "can you rerun lugia unseen force, I came in the middle of the stream" | closed lot | ✅ action | ❌ |
| "run the shining dragon" · "could you run that beautiful dragonite?" | queue | ✅ action | ❌ |
| "you will run the stack after the shining?" | queue | ✅ | ❌ |
| "do you have dragonight on the back wall in your shop?" | shop / background stock | ✅ | ❌ |
| "any bubble mew" | catalog | ✅ | ❌ |
| "Back again plz" | presentation request | ✅ prompt | ❌ |
| "how much are you asking?" | **ambiguous** | ❌ must abstain | ❌ |
| "I wana see theory collection" | **unresolvable** | ❌ must abstain | ❌ |

**Only ~22% refer to the item on screen.** The rest are closed lots, the queue, or the wider
catalog — which is the evidence D-16's pace-aware prior needed, and it lands decisively.

Three further patterns:

**Late arrivals are a recurring, trivially serviceable need.** Three instances — *"just came
in"*, *"I came in the middle of the stream"*, plus a third closing-price question. They want
either a **closing price** or a **re-run**. The system watched every lot close; this is a
"what did I miss" feature handed to us by observation.

**The room self-serves, unreliably.** "base set?" was answered by other viewers guessing
"fire red leaf" among themselves. A separate viewer caught the host mid-error: *"you almost
said shining mewtwo."* The community is already doing verification, manually and without
authority — which is the function we propose to make reliable.

**The recurring unanswerable is the back of a card.** Four appearances: "Back again plz",
"is the back clean?" ×2, "is the dark dragonite clean?", plus the liability speech it
produced. Genuinely unanswerable — back condition is observational (D-12). But the copilot
can surface **"3 people want to see the back"**, which is a *presentation prompt* rather than
an answer, and it resolves the friction that generated the disclaimer.

---

## 4. Two constraints that were not in the model

### 4a. Consignment

> *"They are not mine. I can't take an offer like that when they are not mine. I have been
> instructed to consign them and that is what I am doing."*

The seller does not own the inventory and cannot price below what the consignor permits.
D-04's markdown action assumes the seller controls the price. **On consigned stock they do
not** — there is a second, harder floor set by a third party, and breaching it is a breach of
agreement rather than a bad trade.

Requires `consigned: true` and `consignor_floor` on the lot, treated as absolute rather than
advisory.

### 4b. Pre-bidding

> *"They are all prebid — you can prebid on any of them."*

Buyers can bid on lots **before they go live.** Interest in upcoming lots is therefore
measurable *in advance*, which upgrades D-34's lookahead from "assemble evidence early" to a
demand signal:

> **Lot 331 has 6 pre-bids and nothing above it does — pull it forward.**

A `push_lot` proposal driven by observed demand rather than inference.

### 4c. Not all buyer communication is visible

The consignment statement had **no traceable trigger in chat** — it answered something said
elsewhere, likely a DM or private offer. A chat-only system has a blind spot, and the PRD
should say so.

---

## 5. Entity references observed

Every one of these breaks exact-match lookup. All are seeded into `data/card_sets.json`
as test cases.

| Observed | Resolves to | Failure mode |
|---|---|---|
| `Zard` | Charizard | nickname |
| `big boy gengar` | Gengar (VMAX?) | descriptor that is not a card attribute |
| `shining dragon` / `the shining` | Shining Dragonite | description; two spellings, same card |
| `dragonight` | Dragonite | misspelling |
| `Entai` | Entei | misspelling |
| `gengar red fire` | Gengar (FireRed & LeafGreen) | word order scrambled |
| `bubble mew` | Mew (which one?) | descriptor + family name |
| `lugia unseen force` | Lugia (EX Unseen Forces) | set name singularised |
| `pokemon delta species` | EX Delta Species | set referenced as a card |
| `pokermuns` | Pokémon | phonetic spelling |
| `mew` | **a family, not a card** | under-specified → must abstain |

---

## 6. Still unknown

- Whether promotion causes heat or follows it (see 2a).
- The bell chime. Rang mid-sequence on lot 4, absent on lot 6 — **abandoned as a red
  herring**, probably an unrelated platform notification.
- Whether the extension threshold is fixed or scales with the base timer.
- Whether lot #328 is per-show or cumulative across shows.
- Any measurement of how many messages the chat carried overall — the question counts above
  are absolute, not rates, so "latent demand" during the 4-minute absence remains suggestive
  rather than demonstrated.
