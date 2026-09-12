# Domain Primer: Pokémon Cards for SideStage

**Read time: ~30 min.** This is everything you need to hold a technical conversation about
what SideStage grounds its answers in. It doubles as the specification for the claim
verifier — the table in §7 *is* the verifier registry.

**Honesty marker.** Facts below are marked `[F]` (well-established, safe to assert),
`[C]` (collector terminology — real, but not an official record), `[M]` (modeled for this
prototype — plausible, but verify before citing externally), or `[O]` (observed in the
field, with the date). The seeded catalog in `data/` is modeled from public collector
references. It is not licensed eBay or TCGplayer data, and the repo says so.

---

## 1. Why cards, in one paragraph

eBay Live — eBay's livestream shopping surface — is card-led, and eBay owns TCGplayer, the
dominant trading-card marketplace and price reference `[F]`. Cards are the best possible
domain for a grounded-reply system because a card's identity is a **tuple of discrete,
checkable attributes**, its price is driven by **recent sold comps for that exact tuple**,
and being wrong is **expensive and irreversible**. Telling a buyer a $2,000 card is 1st
Edition when it isn't is not a tone problem. It's a return, a dispute, and a seller-rating
hit.

## 2. How a card is identified

```
(game, set, card_number, name, variant, finish, language, grade)
```

Every one of those changes the price. `Charizard` alone is not an identity — there are
hundreds of Charizards. `Charizard VMAX, Champion's Path, 074/073, Secret Rare, English,
PSA 10` is an identity, and it has comps.

**English Pokémon eras** `[F]`:

| Era | Years | Sets (partial) |
|---|---|---|
| WotC | 1999–2003 | Base Set, Jungle, Fossil, Base Set 2, Team Rocket, Gym, Neo, Legendary Collection, e-Card (Expedition / Aquapolis / Skyridge) |
| Nintendo / TPCi | 2003– | EX, Diamond & Pearl, Platinum, HGSS, Black & White, XY, Sun & Moon, Sword & Shield, Scarlet & Violet |

## 3. The variant axis — where the hallucinations live

This is the single most important section. These are the attributes a language model will
confidently invent.

**1st Edition** `[F]` — a small stamp below-left of the art box. English sets carried it
from Base Set through the e-Card era and **it was discontinued for English starting with
the EX series in 2003.** Japanese printings continued using 1st Edition markings much
longer, so *the rule is language-scoped*.

> **The demo case.** Champion's Path is a 2020 Sword & Shield set. No English card in it
> was ever printed with a 1st Edition stamp. A viewer asks "is that 1st edition?", the
> model — trained on a corpus where "1st edition Charizard" is a wildly common phrase —
> says yes. The verifier blocks it, because the set catalog says that variant does not
> exist for that print run.

**Shadowless** `[F]` — refers to the missing drop shadow along the right edge of the art
frame. It is a **Base Set (English) attribute only.** Base Set printed in order:
1st Edition Shadowless → Shadowless Unlimited → Unlimited (with shadow). A "shadowless"
claim on any other set is impossible.

**Reverse holo** `[F]` — the *non-art* portion of the card is foiled. Introduced with
Legendary Collection (2002). A reverse-holo claim on Base Set / Jungle / Fossil is
impossible.

**Modern rarity finishes** `[F]` — Full Art (FA), Alt Art (AA), Secret Rare, Rainbow Rare,
Gold, Illustration Rare / Special Illustration Rare (SIR, Scarlet & Violet era).

**Cosmos holo** `[C]` — the scattered-star foil pattern. **Galaxy swirl / "swirl"** `[C]` —
a foil printing artifact, prized on vintage.

> **The design rule that falls out of this.** Some attributes are **authoritative** — they
> live in a record (set, number, 1st Edition, grade, cert number). Others are
> **observational** — they exist only in the physical card in the seller's hand (swirl,
> print lines, whitening, centering on a raw card). *The copilot must never assert an
> observational attribute.* It defers to the seller: "the host will check that on camera."
> This is enforced as `attribute_authority: record | observational` on every attribute,
> and it is a verifier rule, not a prompt suggestion.

## 4. The grading axis

Grading is third-party encapsulation and scoring. It swings price by 10–100×, so **every
price statement must be grade-matched.**

| Grader | Scale | Notes |
|---|---|---|
| **PSA** | 1–10 whole grades, plus 1.5 `[F]` | No 8.5 / 9.5. Qualifiers appended: `PSA 8 (OC)` — OC off-center, ST stain, MK marks, MC miscut, PD print defect. Cert number on the label, publicly verifiable. |
| **BGS** (Beckett) | 1–10 in 0.5 steps `[F]` | Four subgrades: Centering, Corners, Edges, Surface. 9.5 = Gem Mint, 10 = Pristine, **Black Label** = 10 with all four subgrades 10. |
| **CGC** | 1–10 in 0.5 steps `[F]` | CGC 10 Pristine; "Perfect 10" when all subgrades are 10. |
| **Raw** (ungraded) | NM / LP / MP / HP / DMG `[F]` | TCGplayer condition ladder. Near Mint → Damaged. |

**Population report ("pop")** `[F]` — how many of that card the grader has graded at each
level. Standard phrasing: *"Pop 412, 38 higher."* Drives scarcity premium. It is
third-party data that **changes over time**, so it must carry an as-of date. A pop quoted
without a date is a bug.

**Centering** `[F]` — expressed as a ratio, `55/45` left-right, `60/40` top-bottom, quoted
front and back. On a BGS slab it's an explicit subgrade. On a PSA slab it's only implied by
the grade. On a raw card it is observational — see §3.

## 5. The price axis — comps

A comp is a **recent sold price for the exact (card, grade) tuple.** Sources: eBay sold
listings, TCGplayer, PSA APR, 130point `[F]`.

Three things make comps statistically dangerous, and all three become verifier rules:

1. **Grade-matching.** A PSA 9 comp tells you almost nothing about a PSA 10 price.
2. **Sample size.** Two sales is not a market. One of them may have been a shill bid, a
   bundled lot, or a friend.
3. **Recency.** Card prices move fast. A six-month-old comp is a historical note.

> **The rule: never quote a bare comp.** The model will want to write "these go for about
> $400." It gets blocked. The permitted form is a range with its sample size and window:
> *"last 7 sold between $380 and $420, past 90 days."* Minimum `n ≥ 5` within 90 days,
> grade-matched, or the claim does not ship.

## 6. How an eBay Live card show actually runs

The seller works through **lots** in sequence — holding each to the camera while chat asks
questions, and answering or missing them. A **lot** is one unit being sold: one slab, one
raw card, one sealed box. The word comes from auction houses. Note it's slightly
overloaded — on eBay proper, "lot" can also mean a multi-item bundle ("lot of 50 commons").
In this system, *lot* always means "the thing currently being sold."

### 6.1 The two formats

**Format is how the item is sold**, and it is load-bearing rather than cosmetic `[F]`.

**Auction lot.** Bidding opens, the highest bid when the timer ends wins. On a live show
the timer is short — 30 to 90 seconds — versus days on ordinary eBay. The price is not one
number; it's four live values: `starting_bid`, `current_bid`, an optional hidden `reserve`
(a minimum below which the seller need not sell), and `ends_at`. **Sniping** `[F]` is
bidding in the final seconds.

**BIN drop.** BIN is **Buy It Now** — eBay's term for fixed price `[F]`. A **drop** is
livestream vocabulary for releasing something for sale at a specific moment. The host says
"this one's $120, dropping now," the listing goes live, first to hit buy gets it. One
authoritative `price`, a `quantity`, sometimes **Best Offer** enabled (the buyer proposes,
the seller accepts, declines, or counters). The scramble is the point — urgency is much of
why live commerce works at all.

**Break** `[F]`. The seller opens sealed product live; buyers pre-purchased slots (a team,
a set, a "random"), and whatever is pulled goes to the slot holder. The chat isn't product
questions at all — it's slot claims and reactions to pulls. Different economics, different
rhythm, different product. **Out of scope for v1**, and the PRD says so.

**Mystery / gamified auction** `[O, 2026-09-11]`. Observed on eBay Live: "$1 START PSA 10
MYSTERY SLABS — win the auction, spin the wheel, land on a slab." The lot is a *concealed*
item; the buyer bids on a slot and a wheel decides which card they receive. Mechanically an
auction (proxy "Max bid", countdown, high-bidder trophy) but economically a cousin of a
break.

**Out of scope, and for a sharper reason than breaks:** there is no listing record to ground
against, because concealment *is* the product. Nobody asks "is that 1st edition?" — they
cannot see the card. Observed chat on such a stream was odds talk ("that's gambling for
you"), not product questions. A grounded-reply copilot has nothing to do here. Naming this
segment as deliberately unserved belongs in the PRD: it shows the wedge was chosen rather
than stumbled into.

Most real shows are **mixed**: auctions for the good material, BIN for volume. For
observation purposes, what matters is that the lots are *identified* — the card is visible,
the lot title names a specific card and grade, and chat asks about that card.

### 6.2 Why the format distinction drives the build

| | Auction lot | BIN drop |
|---|---|---|
| What "price" means | 4 live values, moving mid-request | 1 exact number |
| Dominant chat noise | bid chatter — "I'm in at 40", "who sniped me" | "is it still available?" |
| Misclassification risk | bid chatter reads as `buy_commit` | — |
| Legal write actions | none in v1 (see D-03) | markdown, quantity adjust |
| Typical lot dwell | 1–10 min, **but see below** | ~30 s |
| Q&A density | higher — people ask more before bidding than before clicking buy | lower |

### 6.2a Pace is a third axis, and it was badly underestimated `[O, 2026-09-11]`

The table above treats dwell as a property of format. Field observation shows **pace varies
far more within a format than between formats.** A Whatnot Pokémon slab auction ran lots at
**3 to 10 seconds each** — nothing stayed on screen longer.

At that pace something structural happens: **per-lot Q&A becomes impossible for anyone.** A
viewer needs 3–5 seconds to type. By the time any question about the current lot arrives, the
lot is gone. This is not a latency problem to optimise — it is a target that no longer exists.

So the observed traffic on a fast show is not about the item on screen at all. It is
*"is X coming up later"* and *"any more Ys"* — **the queue and the catalog.** Three regimes:

| Pace | Lot dwell | What questions refer to |
|---|---|---|
| Slow | 1–10 min | the item on screen; classic grounding |
| Moderate | 30 s – 1 min | mixed; the item, plus recently-passed lots |
| **Rapid fire** | **3–10 s** | **the upcoming queue and general inventory** |

The mechanism is identical across all three — resolve the entity, assemble evidence, verify
the claims. Only the **default referent** moves, which is why D-16's prior became pace-aware
rather than being thrown away. And because the lot queue is known in advance, the fast regime
is the one where evidence can be assembled *before* a lot goes live: rapid fire makes latency
easier, not harder (D-34).

**Always read a dwell number next to its format and pace.** Read alone, "40 seconds" looks
like a falsified cache design rather than one conditional layer.

### 6.2b Four consequences, each already reflected in a decision

1. **Chat mix is format-dependent.** Which intent classes dominate depends on what's being
   run, which is why the observation worksheet records format alongside the tallies.
2. **Price verification is format-dependent.** On a BIN lot a price claim checks against one
   number. On an auction it checks against bid state that can change between evidence
   assembly and verification — which is exactly the mid-request staleness case in D-09.
   "Will you do $400?" is a different question in each format.
3. **Write actions are format-gated.** Markdown is meaningless on an auction — you cannot
   lower a bid. Quantity adjustment is meaningless on a single-card auction lot. D-03 scopes
   price and stock writes to BIN lots for this reason, not for convenience.
4. **Dwell time is format-dependent, and this is a measurement trap.** D-19's second cache
   breakpoint assumes a pinned lot stays put for 2–10 minutes. A pure high-speed BIN show
   would show ~40-second dwell and look like it falsifies that — when it only says "this
   format has short dwell." Always read dwell next to format.

### 6.3 Everything else that comes up constantly

- **Combined shipping** is asked on nearly every lot. Highest-volume policy question in the
  domain, and therefore the first intent class that earns promotion up the automation
  ladder (D-22).

  **Field evidence** `[O, 2026-09-11]`: a seller with 75k positive feedback was running a
  permanent on-screen overlay reading `SHIPPING $5.99 THEN $0.50c ALL RECURRING ORDERS!!`.
  That is scarce screen real estate — the same pixels that could show the card — spent
  pre-empting a single question. A seller does not do that until answering it live has
  become untenable.

  This shows sellers have already built a crude version of the feature themselves — a static
  overlay is an un-targeted, un-grounded, un-measurable auto-reply. Worth quoting in the PRD,
  and worth being honest that the incumbent solution is free.

  > ### ⚠ Contradicted by our own data `[O, 2026-09-12]`
  >
  > **"Asked on nearly every lot" is inherited domain lore, and 477 messages of real chat do
  > not support it.** `shipping_returns_q` appears **zero times** across all three transcribed
  > segments — before, middle and after — of a full auction show.
  >
  > The "after" segment was transcribed specifically to test the obvious explanation, that
  > shipping questions cluster at end-of-show when invoices go out. They did not appear there
  > either. That leaves two live hypotheses:
  >
  > 1. **Sellers pre-empt it so effectively that the question stops being asked.** The overlay
  >    above, and this show's `NO CANCEL` listing titles plus a spoken policy statement between
  >    lots, are all suppression. On that reading the pain is real and already solved — badly,
  >    but solved — and the overlay is evidence *against* the wedge rather than for it.
  > 2. **It happens in DMs.** We already know chat is not the whole channel: the consignment
  >    remark answered something with no visible trigger.
  >
  > **Consequence for D-22.** The automation ladder promotes `shipping_returns_q` first on the
  > grounds that it is highest-volume and lowest-risk. The low-risk half holds. The
  > high-volume half is now unevidenced, so the promotion order has to be re-argued from data
  > rather than from this paragraph. On observed volume, `request` (5.9%) and
  > `availability_q` (2.3%) are the classes that actually occur.
  >
  > Kept rather than deleted, because the gap between what the domain "knows" and what the
  > data shows is exactly the sort of thing a reviewer should be able to see us catch.
- **eBay Authenticity Guarantee** `[M]` — eBay authenticates eligible trading cards above a
  value threshold (modeled here as $250 US). **eBay Vault** `[F]` is eBay's storage and
  authentication service. Treat the threshold as modeled; the *mechanism* — a policy claim
  gated on item value — is the point.

## 7. Claim → authority → rule (this is the verifier registry)

| Claim type | Authority | Rule |
|---|---|---|
| set / number / name | listing record | Must match the record exactly. |
| **1st Edition** | set catalog, language-scoped | Hard block if the set never printed the variant in that language. |
| **Shadowless** | set catalog | Base Set English only. Hard block elsewhere. |
| **Reverse holo** | set catalog | Not printed before Legendary Collection. |
| grade + grader | listing record | Must match. Any grade claim requires a cert number present. |
| centering, on a slab | BGS subgrade if present | Never assert a ratio unless a subgrade exists. |
| **centering (raw), swirl, print lines, whitening** | observational | **Never assert.** Defer to the host. |
| **pop count** | third-party, time-varying | Must carry an as-of date. Block if staler than the configured window. |
| **comp price** | comps table | `n ≥ 5`, within 90 days, grade-matched. Render as a range with `n`. Never a bare number. |
| availability / qty | inventory | No unbounded quantifier ("plenty", "tons") below threshold. |
| current price / bid / reserve | lot record | Exact match. On auction lots, bid state is live — re-read at verify time. |
| shipping / returns / authenticity | policy corpus | Must cite a clause. Authenticity claim additionally gated on item value. |

Anything containing a number, a superlative, or a commitment verb that carries **no backing
claim id at all** is blocked by default. That is the backstop for claim types nobody
enumerated — the system fails closed.

## 8. Chat glossary

You will read a lot of synthetic chat. This is the vocabulary.

| Term | Meaning |
|---|---|
| **singles** | individual cards sold one at a time — the format this product requires, because each lot is identified |
| **sealed** | unopened product: boxes, packs, ETBs. A chance, not a known card |
| **lot / bundle** | many cards sold together ("lot of 50 commons") |
| **rip** | to open sealed product on stream |
| **memorabilia** | eBay's umbrella category — jerseys, autographs, balls; cards are a subset of it, not a synonym |
| gem / gem mint | PSA 10 |
| slab | a graded card in its sealed plastic case |
| raw | ungraded |
| pop | population report count |
| comps | recent sold prices |
| BIN | buy it now |
| chase / GOAT | the most desirable card in a set |
| hit | a valuable pull from a pack |
| whitening | edge wear exposing the white card core |
| print lines | surface printing defect |
| OC | off-center |
| FA / AA / SIR | full art / alt art / special illustration rare |
| ETB | Elite Trainer Box (sealed product) |
| "$350 shipped" | price inclusive of shipping |
| PC | personal collection — not for sale |
| sniped | outbid in the final seconds |

## 9. What to say when asked "why this user"

> eBay Live's volume is card-led, and I picked the category where attribute claims and
> price claims are both high-value and mechanically verifiable. A wrong "1st edition" on a
> $2,000 card is an unrecoverable error — a return, a dispute, and a rating hit. That is
> the exact failure the verifier exists to catch, and the set catalog gives me a real
> authority to check it against rather than a vibe.
