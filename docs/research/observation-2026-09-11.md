# Field observation — 2026-09-11

First contact with real live-selling chat. **These are cursory observations made while
watching, not the structured tally.** A 45-minute recording exists and has not yet been
scrubbed, so everything below is an impression rather than a measurement. Numbers from the
tally will supersede this file; nothing here should be quoted in the PRD as a statistic.

What it *is* good for: several of these observations falsify assumptions that were written
down as settled, and those corrections should not wait for the tally.

---

## Sessions

| Platform | Show | Viewers | Format | Outcome |
|---|---|---|---|---|
| eBay Live | "$1 START PSA 10 MYSTERY SLABS" | 130 | auction, **mystery** | skipped — concealed item |
| eBay Live | "Friday Night Memorabilia" | 182 | auction, **mystery**, memorabilia | skipped — concealed + not cards |
| Whatnot | Pokémon slab auction, rapid fire | ~1,000 | auction, singles | **45 min recorded** |

**Availability finding.** Pokémon singles shows above 100 concurrent viewers were hard to
find on eBay Live on a Friday evening; Whatnot was markedly busier. Two of two eBay Live
shows sampled were mystery/gamified formats. n is far too small to conclude anything about
the share of eBay Live that is concealed-format, but it is worth counting deliberately.

---

## Finding 1 — lots run 3 to 10 seconds

The single most consequential observation. Nothing in the recorded show stayed on screen
longer than about ten seconds.

**Falsifies** the primer's claim that auction lots run 2–3 minutes, and D-19's assumption
that a pinned lot is stable for 2–10 minutes while questions accumulate about it.

**The deeper consequence:** at this pace, per-lot Q&A is structurally impossible *for
anyone*. A viewer takes 3–5 seconds to type. By the time any question about the current lot
arrives, that lot is gone. This is not a latency problem to optimise — it is a target that
does not exist.

## Finding 2 — the questions that exist are about the queue, not the lot

Observed forms: *"is X coming up later"*, *"any more Ys"*. These refer to **upcoming lots
and general inventory**, not to the item on screen.

This is the resolution of the fork. Rapid-fire is not a segment to walk away from; it is a
segment where the **default referent is different**. Same mechanism — resolve entity,
assemble evidence, verify claims — with the prior pointed at the queue and the catalog
rather than the active lot.

It also inverts the latency story: the lot queue is known in advance, so evidence for
upcoming lots can be assembled *before* they go live. The format that looked like it would
break the latency budget is the one where pre-fetch wins.

## Finding 3 — chat is a social space, not a question queue

Described as "a stream of consciousness of the group." Message types observed, none of which
were in the taxonomy:

| Observed | Notes |
|---|---|
| Banter / light trolling with the seller | Social. Makes the show work. Should never be auto-replied to. |
| Single-character messages | Droppable on a length check before any scoring. |
| **System / platform messages** | "X won", joins, unlocks. **Not user messages at all.** Some are genuine signal — a win is a purchase event. |
| **Cross-user talk** | Viewers addressing each other. May contain question marks while not being addressed to the seller. Surfacing these is a false positive. |
| Emoji reactions (🔥) | Noise for the reply queue; **signal** for the engagement / top-moment layer. |
| **Bare-noun entity mentions** ("mew") | See finding 4. |
| Genuine questions | Present, and of the queue/inventory kind above. |

## Finding 4 — buying intent arrives as a bare noun

Viewers typed things like **"mew"** — no verb, no question mark, no subject. It almost
certainly means *"do you have a Mew"* or *"I want the Mew"*, and it is among the most
commercially valuable traffic in the room.

**This breaks the obvious classifier.** Any cheap heuristic keyed on question marks or
interrogative words drops every one of these.

**And the entity itself is unresolvable.** Mew appears across dozens of sets over 25 years —
Mew ex, Mew VMAX, the Celebrations reprint, the 151 set, numerous promos — and is one
keystroke from Mewtwo. A bare "mew" names a family, not a card. The correct reply is often
*"which Mew?"*, and a system that confidently picks one is worse than one that asks.

## Finding 5 — a seller hard-coding an answer into their own stream

`[eBay Live, "$1 START PSA 10 MYSTERY SLABS"]` — a permanent on-screen overlay reading
`SHIPPING $5.99 THEN $0.50c ALL RECURRING ORDERS!!`, from a seller with 75k positive
feedback.

Scarce screen real estate — the same pixels that could show the card — spent pre-empting one
question. Sellers do not do that until answering it live has become untenable. It cuts both
ways: strong evidence the pain is real and concentrated in shipping, *and* evidence that a
free incumbent solution already exists. A static overlay is a crude auto-reply: un-targeted,
un-grounded, un-measurable. The product's claim has to be that the same job done
per-question and grounded in the actual order beats a billboard.

## Finding 6 — questions about background inventory

`[eBay Live, "Friday Night Memorabilia"]` — active lot #021, and a viewer asked *"How much
for ohtani 👀"*, referring to a framed jersey visible **behind the seller**, not the item
being sold. Within three messages of chat.

Sellers have stock physically visible behind them and buyers ask about what they can see.
High buying intent, and a naive pinned-lot prior either misresolves it or drops it.

---

## What this changes

| Decision | Change |
|---|---|
| D-13 | Taxonomy grows: `system_event`, `cross_user`. Bare-noun intent is a first-class case. |
| D-15 | Cheap arm gains a catalog-entity trie and a length filter. Question-mark heuristics are insufficient. |
| D-16 | Pinned-lot prior becomes **pace-aware** rather than unconditional. |
| D-19 | Per-lot cache breakpoint becomes pace-conditional; it earns nothing on fast shows. |
| D-34 | New — queue lookahead pre-fetch. |
| Primer §6.2 | Dwell figures corrected; rapid-fire added as a format. |

## What is still unknown

- The actual intent distribution. No tally has been run.
- Chat velocity as a number. "Busy" is not a measurement.
- Whether rapid-fire is representative of Whatnot Pokémon slabs or was one show's style.
- Whether eBay Live Pokémon singles shows are genuinely small or Friday evening was thin.
- The miss rate — how much a host actually fails to answer. Untested.
