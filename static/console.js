/* SideStage operator console.
 *
 * Preact + htm from a CDN, no build step (D-07). The whole front end is this
 * file: a reviewer can read it top to bottom and there is nothing generated in
 * between what is written and what runs.
 *
 * The three columns are the three claims the project makes, in order:
 *
 *   LEFT    every message, including the dropped ones, with the features that
 *           dropped them. The interpretability claim, visible rather than
 *           argued — D-15 chose a linear model so this column could exist.
 *   MIDDLE  what survived, ranked, with duplicate counts.
 *   RIGHT   the draft, the claims, the fact each one cites, and the verifier's
 *           verdict. A blocked reply is shown, not hidden: the operator sees
 *           the refused text and the reason beside it (D-23).
 */

import { h, render, Fragment } from "preact";
import { useState, useEffect, useCallback } from "preact/hooks";
import htm from "htm";

const html = htm.bind(h);

const api = async (path, body) => {
  const r = await fetch(path, body === undefined
    ? {}
    : { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(body) });
  if (!r.ok && r.status !== 404) throw new Error(`${path} -> ${r.status}`);
  return r.json();
};

/* ---------------------------------------------------------------- theme */

/* Stored choice wins; with none the page follows the OS. Wrapped because
   localStorage throws outright in some contexts (private windows, blocked site
   data) and a theme toggle must never be what stops the console rendering. */
const THEME_KEY = "sidestage.theme";

function readTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
}
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  try { t ? localStorage.setItem(THEME_KEY, t) : localStorage.removeItem(THEME_KEY); }
  catch (e) {}
}
function currentTheme() {
  const stored = readTheme();
  if (stored) return stored;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}
applyTheme(readTheme());   // before first paint, so there is no flash

function ThemeToggle() {
  const [t, setT] = useState(currentTheme());
  const flip = () => { const n = t === "dark" ? "light" : "dark"; applyTheme(n); setT(n); };
  return html`
    <button class="icon ghost" onClick=${flip} title=${`Switch to ${t === "dark" ? "light" : "dark"} mode`}
            aria-label=${`Switch to ${t === "dark" ? "light" : "dark"} mode`}>
      ${t === "dark" ? "☀" : "☾"}
    </button>`;
}

/* ---------------------------------------------------------------- header */

/* The nudge. Rendered beside the lot rather than in a panel, because the whole
   point is that it is absorbed at a glance while the seller is talking (D-35:
   ~5 s of reset window minus ~3 s to read and begin speaking). Absent most of
   the time, which is the design — a nudge layer that always has something to
   say is one the operator learns to ignore. */
function Nudge({ nudge }) {
  if (!nudge) return null;
  return html`
    <div class=${"nudge " + nudge.moment} title=${nudge.why}>
      <span class="tag">${nudge.moment}</span>
      <b>${nudge.text}</b>
    </div>`;
}

/* The auction, driveable (B-128).
 *
 * `app/moments.py` classifies hot / stalled / normal and `Session.nudge()`
 * turns that into one glanceable line — and until this existed a reviewer
 * could not make it happen. Every lot that classifies as hot or stalled in the
 * shipped catalog is already SOLD, so the console showed the nudge layer
 * permanently empty and the eval suite behind it was unreachable by clicking.
 *
 * Two buttons because a bid and a timer extension are DIFFERENT EVENTS: a bid
 * must raise the price, so if every extension carried one the price could never
 * stand still and `stalled` — bids that stopped arriving — would be
 * unreachable by construction. "Timer +" sends no amount, which is exactly what
 * a stall is. */
function Auction({ lot, busy, onBid, onExtend }) {
  if (!lot || lot.format !== "auction" || !["live", "queued"].includes(lot.status))
    return null;
  const next = Math.round((lot.current_bid || lot.price || 100) * 1.05);
  return html`
    <div class="auction">
      <button class="ghost" disabled=${busy} onClick=${() => onBid(next)}
              title="a bid, which must raise the price">
        Bid $${next}
      </button>
      <button class="ghost" disabled=${busy} onClick=${onExtend}
              title="the timer extends with no new bid — this is what a stall is">
        Timer +
      </button>
    </div>`;
}

/* Which lot is on screen.
 *
 * Not cosmetic: `assemble` builds the evidence block around the ACTIVE lot, so
 * the same question against a different lot is a different set of facts and a
 * different answer. The curated demo cases each have a lot they are about — ask
 * "is that 1st edition?" with the Base Set Charizard up and you are asking
 * about a different card than the doc means. Without this control the operator
 * could drive the auction but never change what the copilot was looking at.
 */
function LotPicker({ lots, lotId, busy, onPick }) {
  if (!lots || !lots.length) return null;
  return html`
    <select class="lotpick" disabled=${busy} value=${lotId || ""}
            onChange=${e => onPick(e.target.value)}
            title="the active lot — the evidence block is built around it">
      ${lots.map(l => html`
        <option key=${l.id} value=${l.id} selected=${l.id === lotId}>
          ${l.status === "live" ? "● " : ""}${l.title.slice(0, 42)}
        </option>`)}
    </select>`;
}

function Header({ stats, lot, lots, nudge, busy, onReplay, onReset,
                  onBid, onExtend, onPickLot }) {
  return html`
    <header>
      <div class="brand">Side<span>Stage</span></div>
      ${lot && html`
        <div class="lot">
          <span class="fmt">${lot.format}</span>
          <b>${lot.title}</b>
          <span class="money">
            ${lot.current_bid != null ? `bid $${lot.current_bid}`
              : lot.price != null ? `$${lot.price}` : "—"}
          </span>
        </div>`}
      <${LotPicker} lots=${lots} lotId=${lot && lot.id} busy=${busy}
                    onPick=${onPickLot} />
      <${Nudge} nudge=${nudge} />
      <${Auction} lot=${lot} busy=${busy} onBid=${onBid} onExtend=${onExtend} />
      <div class="spacer"></div>
      <div class="counters">
        <div><b>${stats.seen}</b><span>seen</span></div>
        <div><b>${stats.dropped}</b><span>dropped</span></div>
        <div><b>${stats.surfaced}</b><span>surfaced</span></div>
        <div><b>${Math.round((stats.escalation_rate || 0) * 100)}%</b><span>escalated</span></div>
        <div><b>${stats.sent}</b><span>sent</span></div>
      </div>
      <button onClick=${onReplay} disabled=${busy}>
        ${busy ? "replaying…" : "Replay 30 real messages"}
      </button>
      <button class="ghost" onClick=${onReset}>Reset</button>
      <${ThemeToggle} />
    </header>`;
}

/* ------------------------------------------------------------- chat log */

/* A drop reason looks like "+0.33 second_person". Colour by sign so the
 * operator can see at a glance what argued for and against surfacing. */
function Reason({ text }) {
  const cls = text.startsWith("+") ? "pos" : text.startsWith("-") ? "neg" : "vetoed";
  return html`<span class=${cls}>${text}</span>`;
}

/* The chat, both directions.
 *
 * The seller's sent replies are rendered from the LEDGER rather than from a
 * second copy kept in the log. The ledger is the record that something went to
 * a buyer (`send_reply`, with the verdict and whether the operator overrode a
 * block), so sourcing the chat from it means the two can never disagree about
 * what was said — a chat showing a reply the ledger does not record, or the
 * reverse, is the kind of divergence this project spends its time removing.
 *
 * `LoggedMessage` stays what it is: one BUYER message and what triage decided
 * about it. Giving it an `author` field would have meant a route, a score and
 * an intent on a message triage never saw — an untyped hole in the one
 * structure the left column exists to explain.
 */
function ChatLog({ log, ledger }) {
  const sent = new Map();
  for (const e of ledger || []) {
    if (e.action !== "send_reply" || !e.card_id) continue;
    if (!sent.has(e.card_id)) sent.set(e.card_id, []);
    sent.get(e.card_id).push(e);
  }
  // A card can be several buyers asking the same thing (`count` > 1), so the
  // reply is anchored after the LAST message that fed it, not the first.
  const lastForCard = new Map();
  log.forEach((m, i) => { if (m.card_id) lastForCard.set(m.card_id, i); });

  const rows = [];
  log.forEach((m, i) => {
    rows.push(html`
      <div class=${"msg " + (m.surfaced ? "surfaced" : "dropped")} key=${"m" + m.seq}>
        <div class="t">${m.text}</div>
        <div class="why">
          <span>${m.score.toFixed(2)}</span>
          ${m.reasons.map(r => html`<${Reason} text=${r} />`)}
          ${m.escalated && html`<span>· model</span>`}
        </div>
      </div>`);
    if (m.card_id && lastForCard.get(m.card_id) === i) {
      for (const e of sent.get(m.card_id) || []) {
        rows.push(html`
          <div class="msg out" key=${"s" + e.id}>
            <div class="t">${e.text}</div>
            <div class="why">
              <span>sent</span>
              <span>· ${e.verdict}</span>
              ${e.overridden && html`<span class="ov">· operator override</span>`}
            </div>
          </div>`);
      }
    }
  });

  const nSent = [...sent.values()].reduce((n, a) => n + a.length, 0);
  return html`
    <div class="col">
      <h2>Chat <em>${log.length} in${nSent ? ` · ${nSent} sent` : ""} · dropped shown with reasons</em></h2>
      <div class="scroll">
        ${log.length === 0 && html`<div class="empty">
          Nothing yet. Replay the recorded show, or type a message below the queue.
        </div>`}
        ${rows}
      </div>
    </div>`;
}


/* ---------------------------------------------------------------- queue */

function Queue({ queue, activeId, onPick, onSay }) {
  const [text, setText] = useState("");
  const submit = (e) => {
    e.preventDefault();
    if (!text.trim()) return;
    onSay(text.trim());
    setText("");
  };
  return html`
    <div class="col">
      <h2>Queue <em>${queue.length} open · ranked by intent × demand × recency</em></h2>
      <div class="scroll">
        ${queue.length === 0 && html`<div class="empty">
          No open questions. Everything so far was noise, which is the common case —
          about 84% of real chat.
        </div>`}
        ${queue.map(c => html`
          <div class=${"card" + (c.id === activeId ? " active" : "")}
               key=${c.id} onClick=${() => onPick(c.id)}>
            <div class="top">
              <span class="pill">${c.intent}</span>
              ${c.count > 1 && html`<span class="pill count">×${c.count} asked</span>`}
              ${c.status === "ready" && html`<span class="pill ready">verified</span>`}
              ${c.status === "blocked" && html`<span class="pill blocked">blocked</span>`}
              ${c.status === "drafting" && html`<span class="pill">drafting…</span>`}
            </div>
            <div class="q">${c.text}</div>
            <div class="meta">score ${c.score} · ${c.at.slice(11, 19)}</div>
          </div>`)}
      </div>
      <form onSubmit=${submit} style="padding:10px 12px;border-top:1px solid var(--line)">
        <input type="text" id="say" value=${text} placeholder="Type a buyer message…"
               onInput=${e => setText(e.target.value)} />
      </form>
    </div>`;
}

/* ---------------------------------------------------------------- draft */

function Draft({ card, busy, judged, onDraft, onSend, onDismiss, ledger, actions }) {
  if (!card) {
    return html`
      <div class="col">
        <h2>Draft</h2>
        <div class="scroll">
          <div class="empty">Pick a question from the queue.</div>
          ${actions}
          ${ledger.length > 0 && html`<${Ledger} ledger=${ledger} />`}
        </div>
      </div>`;
  }
  const drafted = card.verdict !== "";
  return html`
    <div class="col">
      <h2>Draft <em>${card.id} · ${card.intent}</em></h2>
      <div class="scroll">
        <div class="block">
          <h3>Buyer asked</h3>
          <div style="font-size:15px">${card.text}</div>
        </div>

        ${!drafted && html`
          <div class="actions">
            <button class="primary" onClick=${() => onDraft(card.id)} disabled=${busy}>
              ${busy ? "assembling evidence, generating, verifying…" : "Draft a reply"}
            </button>
          </div>`}

        ${drafted && html`
          <div class="block">
            <h3>${card.verdict === "blocked" ? "Blocked — not sent" : "Reply"}</h3>
            <${Degraded} card=${card} />
            <div class=${"reply " + (card.verdict === "blocked" ? "blocked" : "ready")}>
              ${card.reply || html`<em style="color:var(--ink-faint)">
                the model returned nothing usable</em>`}
            </div>
          </div>`}

        ${card.violations.length > 0 && html`
          <div class="block">
            <h3>Why it was blocked</h3>
            ${card.violations.map((v, i) => html`
              <div class=${"viol" + (v.severity === "repairable" ? " repairable" : "")} key=${i}>
                <code>${v.code}</code> · ${v.severity}
                <p>${v.message}</p>
              </div>`)}
          </div>`}

        ${card.verdict === "blocked" && card.fallback && html`
          <div class="block">
            <h3>Safe to send instead</h3>
            <div class="reply">${card.fallback}</div>
          </div>`}

        ${card.claims.length > 0 && html`
          <div class="block">
            <h3>Claims, and the fact each one cites</h3>
            <table class="claims">
              <thead><tr><th>fact</th><th>type</th><th>asserted</th></tr></thead>
              <tbody>
                ${card.claims.map((c, i) => html`
                  <tr key=${i}>
                    <td class="fid">${c.fact}</td>
                    <td class="ty">${c.type}</td>
                    <td>${c.value}</td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}

        ${card.facts.length > 0 && html`
          <div class="block">
            <h3>Evidence assembled before generation</h3>
            <ul class="facts" style="margin:0;padding:0">
              ${card.facts.map(f => html`
                <li key=${f.id}>
                  <span class="id">[${f.id}]</span> ${f.note}
                  <span class="auth">· ${f.authority}</span>
                </li>`)}
            </ul>
          </div>`}

        ${judged && judged.pending === false && !judged.responsive && html`
          <div class="block">
            <h3>Second opinion</h3>
            <div class="viol repairable">
              <code>unresponsive</code> · advisory
              <p>${judged.why}</p>
              <p style="color:var(--ink-faint);margin-top:5px">
                Every claim was verified. This is the check that cannot be made
                from claims — whether the reply answers what was asked.
                <b>A warning, not a block.</b>
              </p>
            </div>
          </div>`}

        ${drafted && html`
          <div class="actions">
            <button class="primary" onClick=${() => onSend(card.id)}
                    disabled=${card.status === "sent"}>
              ${card.status === "sent" ? "Sent"
                : card.verdict === "blocked" ? "Send the safe reply" : "Send"}
            </button>
            <button onClick=${() => onDraft(card.id)} disabled=${busy}>Redraft</button>
            <button class="ghost" onClick=${() => onDismiss(card.id)}>Dismiss</button>
          </div>
          <div class="timing">
            ${card.ttft_ms > 0 ? `${card.ttft_ms} ms to first readable token · ` : ""}
            ${card.total_ms} ms to sendable
            ${card.attempts > 1 ? ` · ${card.attempts} attempts (one repair)` : ""}
          </div>`}

        ${actions}
        ${ledger.length > 0 && html`<${Ledger} ledger=${ledger} />`}
      </div>
    </div>`;
}

/* B-98/B-128. A replay fixture miss returns "Let me check that and come back to
 * you" with NO claims — which verifies clean precisely because it asserts
 * nothing. Without this badge the console rendered a canned sentence as a
 * verified pass, and a reviewer running with no credential saw a system that
 * appeared to answer every card and verify every one.
 *
 * A pass earned by having nothing to check is not a pass earned by checking. */
function Degraded({ card }) {
  if (!card || !card.degraded) return null;
  return html`
    <div class="degraded" title=${card.degraded_note || ""}>
      <b>no model answered</b> — replay fixture miss or tripped breaker. This
      reply asserts nothing, so it verifies clean by default, not by checking.
    </div>`;
}

/* The write path, reachable (B-128).
 *
 * `app/actions/` implements D-21 — propose, snapshot preconditions, confirm,
 * execute under an idempotency key, READ BACK, journal the inverse — and the
 * console called none of it. The brief grades on "a concrete failure path",
 * and a reviewer clicking through the product could not reach one.
 *
 * The read-back is the part worth watching. A write that returns success and a
 * read that disagrees is what real marketplaces produce, and a divergence is
 * REPORTED rather than retried: retrying a write that may have landed is how
 * you double-apply. Start the server with SIDESTAGE_FAULTS=1 and this panel
 * shows lost responses being replayed under their original key.
 */
function Actions({ lots, busy, onAct }) {
  const shop = (lots || []).filter(l => l.price != null && !l.unreadable);
  if (!shop.length) return null;
  // Prefer a lot with a floor: the floor is what makes the refusal legible.
  const lot = shop.find(l => l.floor_price != null) || shop[0];
  const id = lot.lot_id;
  const floor = lot.floor_price;
  // A markdown the floor ALLOWS: halfway between the floor and today's price,
  // so it clears the guard on every fixture row rather than by luck.
  const legal = floor != null
    ? Math.max(Math.ceil(floor) + 1, Math.round((lot.price + floor) / 2))
    : Math.max(1, Math.round(lot.price * 0.9));
  // A markdown the floor REFUSES: one dollar under it. Aimed, not accidental.
  const under = floor != null ? Math.floor(floor) - 1 : null;
  return html`
    <div class="block" style="margin-top:22px">
      <h3>Write path <em>D-21 · propose → confirm → execute → read back</em></h3>
      <div class="actions" style="gap:6px;flex-wrap:wrap">
        <button class="ghost" disabled=${busy}
                onClick=${() => onAct("markdown", { lot_id: id, new_price: legal })}>
          Mark ${id} down to $${legal}
        </button>
        ${under != null && html`
          <button class="ghost" disabled=${busy}
                  onClick=${() => onAct("markdown", { lot_id: id, new_price: under })}>
            Try $${under} — under the $${floor} floor
          </button>`}
        <button class="ghost" disabled=${busy}
                onClick=${() => onAct("adjust_quantity",
                  { lot_id: id, new_quantity: Math.max(0, (lot.quantity || 1) - 1) })}>
          Drop stock by one
        </button>
      </div>
      <div class="hint">
        The second button is refused by <code>FloorPriceViolation</code> before
        anything is written — a precondition, not a rollback. The marketplace
        keeps its own row with its own version counter, which is why a read-back
        can disagree; run with <code>SIDESTAGE_FAULTS=1</code> to see lost
        responses replayed under their original key.
      </div>
    </div>`;
}

/* On-demand product research for the active lot.
 *
 * Everything here is a RECORD, not a generated sentence, which is why it lands
 * in single-digit milliseconds against a 2 s budget — it is the same evidence
 * `assemble` builds before any draft, handed straight back. The latency is
 * printed because a budget a panel cannot show it meets is a target, and this
 * project does not display targets as results.
 *
 * The reserve appears here and is flagged. This is the seller's own console;
 * withholding their own reserve from them would be absurd. `_operator_only` in
 * app/verify.py is what stops it reaching a buyer through a draft.
 */
/* The seller's own position on this lot. OPERATOR ONLY, all of it.
 *
 * Kept out of `Evidence` deliberately and therefore un-citable: no fact id
 * exists for a cost basis, so no claim can point at one and no draft can leak
 * it. The visible marker is the second line of defence, not the first.
 *
 * Margin is null rather than zero when the cost basis is unknown. A margin
 * computed from a missing number is a made-up number, and this panel is the one
 * a seller would price against. */
function Commercial({ c }) {
  if (!c) return null;
  const money = v => v == null ? "—" : "$" + Number(v).toLocaleString();
  const cells = [
    ["floor", money(c.floor_price)],
    ["cost", money(c.cost_basis)],
    ["reserve", money(c.reserve)],
    c.price != null ? ["price", money(c.price)] : ["bid", money(c.current_bid)],
    ["qty", c.quantity ?? "—"],
  ];
  return html`
    <div class="comm">
      <div class="comm-head">
        your position <span class="ov">· operator only, never citable</span>
      </div>
      <div class="comm-grid">
        ${cells.map(([k, v]) => html`
          <div key=${k}><span>${k}</span><b>${v}</b></div>`)}
        ${c.margin_abs != null && html`
          <div key="margin" class=${c.at_floor ? "at-floor" : ""}>
            <span>margin</span>
            <b>${money(c.margin_abs)}${c.margin_pct != null
                 ? ` · ${c.margin_pct}%` : ""}</b>
          </div>`}
      </div>
      ${c.at_floor && html`<div class="hint" style="margin-top:4px">
        at or below the floor — a markdown here is refused by
        <code>FloorPriceViolation</code>.
      </div>`}
    </div>`;
}

function Research({ lotId, busy }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  useEffect(() => { setData(null); }, [lotId]);
  const load = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (data || !lotId) return;
    setLoading(true);
    try { setData(await api(`/api/research/${lotId}`)); }
    finally { setLoading(false); }
  };
  if (!lotId) return null;
  return html`
    <div class="block" style="margin-top:22px">
      <h3>
        Research <em>the record behind this lot, no model call</em>
        <button class="ghost" style="float:right;font-size:11px;padding:3px 8px"
                disabled=${busy} onClick=${load}>
          ${open ? "hide" : "look it up"}
        </button>
      </h3>
      ${open && loading && html`<div class="hint">looking…</div>`}
      ${open && data && html`
        <div class="hint" style="margin-bottom:8px">
          ${data.fact_count} facts ·
          <b>${data.latency_ms} ms</b> against a ${data.budget_ms} ms budget ·
          ${data.within_budget ? "within" : "OVER"}
        </div>
        <${Commercial} c=${data.commercial} />
        ${Object.entries(data.facts).sort().map(([kind, fs]) => html`
          <div class="rfact" key=${kind}>
            <div class="rkind">${kind}</div>
            <div>
              ${fs.map(f => html`
                <div key=${f.id} class="rrow">
                  <span class=${"auth a-" + f.authority}>${f.authority}</span>
                  <span>${f.note}</span>
                  ${f.operator_only && html`<span class="ov"> · operator only</span>`}
                </div>`)}
            </div>
          </div>`)}`}
    </div>`;
}

function Ledger({ ledger }) {
  return html`
    <div class="block" style="margin-top:22px">
      <h3>Ledger</h3>
      <ul class="ledger" style="margin:0;padding:0">
        ${ledger.map(e => html`
          <li key=${e.id}>
            ${e.id} · ${e.at.slice(11, 19)}${e.action ? " · " + e.action : ""} · ${e.status || e.verdict}
            ${e.overridden && html`<span class="ov"> · operator override</span>`}
            ${e.diverged && html`<span class="ov"> · read-back disagreed</span>`}
            <div style="color:var(--ink-faint)">
              ${e.text || e.message || ""}
              ${e.params && html`<span> · ${Object.entries(e.params)
                .filter(([k]) => k !== "lot_id")
                .map(([k, v]) => k + "=" + v).join(" ")}</span>`}
            </div>
            ${e.inverse && html`
              <div class="hint" style="margin-top:3px">
                ${e.inverse.reversible === false
                  ? html`<b>irreversible</b> — ${e.inverse.why}`
                  : html`inverse recorded: ${JSON.stringify(e.inverse.previous)}`}
              </div>`}
          </li>`)}
      </ul>
    </div>`;
}

/* ----------------------------------------------------------------- app */

function App() {
  const [st, setSt] = useState(null);
  const [activeId, setActive] = useState(null);
  const [busy, setBusy] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [judged, setJudged] = useState(null);
  const [lots, setLots] = useState([]);

  const refresh = useCallback(async () => setSt(await api("/api/state")), []);
  useEffect(() => { refresh(); }, [refresh]);
  // The MARKETPLACE's copy, which is deliberately not ours: its own status, its
  // own version counter. That separation is the entire reason a read-back can
  // disagree, so the console shows both rather than merging them (B-128).
  useEffect(() => { api("/api/actions/lots")
    .then(r => setLots(r.lots)).catch(() => {}); }, []);

  // Poll rather than push. The console is one operator on one process, the
  // state is small, and a WebSocket here would be infrastructure in place of
  // a two-line interval (D-07: no ceremony the demo does not need).
  useEffect(() => {
    const t = setInterval(() => { if (!busy && !drafting) refresh(); }, 2000);
    return () => clearInterval(t);
  }, [refresh, busy, drafting]);

  if (!st) return html`<div class="boot">connecting…</div>`;

  const card = st.queue.find(c => c.id === activeId) || null;

  const replay = async () => {
    setBusy(true);
    try { await api("/api/replay", { n: 30, offset: st.stats.seen }); await refresh(); }
    finally { setBusy(false); }
  };
  const say = async (text) => { await api("/api/chat", { text }); await refresh(); };

  /* B-128. Drives the real auction so the moment classifier and the nudge are
   * reachable by clicking. `amount: null` is a timer extension with no bid
   * behind it, which is the only way `stalled` can ever occur — a bid must
   * raise the price, so extensions that carry one can never leave the price
   * standing still. */
  const bid = async (amount) => {
    if (!st.lot) return;
    setBusy(true);
    try { await api(`/api/lot/${st.lot.id}/bid`, { amount }); await refresh(); }
    finally { setBusy(false); }
  };
  const act = async (action, params) => {
    setBusy(true);
    try {
      await api("/api/actions", { action, params });
      setLots((await api("/api/actions/lots")).lots);
      await refresh();
    } finally { setBusy(false); }
  };
  const pickLot = async (id) => {
    if (!id) return;
    setBusy(true);
    try { await api(`/api/lot/${id}`, {}); await refresh(); }
    finally { setBusy(false); }
  };
  const extend = async () => {
    if (!st.lot) return;
    setBusy(true);
    try { await api(`/api/lot/${st.lot.id}/bid`, {}); await refresh(); }
    finally { setBusy(false); }
  };
  const doDraft = async (id) => {
    setDrafting(true);
    setJudged(null);
    try { await api(`/api/cards/${id}/draft`, {}); await refresh(); }
    finally { setDrafting(false); }
    // The second opinion lands while the operator reads (B-32). Poll rather
    // than block the draft on it; a pending judge must never hold up the reply.
    for (let i = 0; i < 15; i++) {
      const j = await api(`/api/cards/${id}/judgement`);
      if (!j.pending) { setJudged(j); return; }
      await new Promise(r => setTimeout(r, 400));
    }
  };
  const doSend = async (id) => { await api(`/api/cards/${id}/send`, {}); setActive(null); setJudged(null); await refresh(); };
  const doDismiss = async (id) => { await api(`/api/cards/${id}/dismiss`, {}); setActive(null); await refresh(); };
  const reset = async () => { await api("/api/reset", {}); setActive(null); await refresh(); };

  return html`
    <div class="shell">
      <${Header} stats=${st.stats} lot=${st.lot} lots=${st.lots} nudge=${st.nudge}
                 busy=${busy} onBid=${bid} onExtend=${extend} onPickLot=${pickLot}
                 onReplay=${replay} onReset=${reset} />
      <div class="cols">
        <${ChatLog} log=${st.log} ledger=${st.ledger} />
        <${Queue} queue=${st.queue} activeId=${activeId} onPick=${setActive} onSay=${say} />
        <${Draft} card=${card} busy=${drafting} judged=${judged} onDraft=${doDraft}
                  onSend=${doSend} onDismiss=${doDismiss} ledger=${st.ledger}
                  actions=${html`<${Fragment}>
                    <${Research} lotId=${st.lot && st.lot.id} busy=${busy} />
                    <${Actions} lots=${lots} busy=${busy} onAct=${act} />
                  <//>`} />
      </div>
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));
