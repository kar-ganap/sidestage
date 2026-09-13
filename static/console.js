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

import { h, render } from "preact";
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

function Header({ stats, lot, busy, onReplay, onReset }) {
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

function ChatLog({ log }) {
  return html`
    <div class="col">
      <h2>Chat <em>${log.length} messages · dropped shown with reasons</em></h2>
      <div class="scroll">
        ${log.length === 0 && html`<div class="empty">
          Nothing yet. Replay the recorded show, or type a message below the queue.
        </div>`}
        ${log.map(m => html`
          <div class=${"msg " + (m.surfaced ? "surfaced" : "dropped")} key=${m.seq}>
            <div class="t">${m.text}</div>
            <div class="why">
              <span>${m.score.toFixed(2)}</span>
              ${m.reasons.map(r => html`<${Reason} text=${r} />`)}
              ${m.escalated && html`<span>· model</span>`}
            </div>
          </div>`)}
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

function Draft({ card, busy, onDraft, onSend, onDismiss, ledger }) {
  if (!card) {
    return html`
      <div class="col">
        <h2>Draft</h2>
        <div class="scroll">
          <div class="empty">Pick a question from the queue.</div>
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

        ${ledger.length > 0 && html`<${Ledger} ledger=${ledger} />`}
      </div>
    </div>`;
}

function Ledger({ ledger }) {
  return html`
    <div class="block" style="margin-top:22px">
      <h3>Ledger</h3>
      <ul class="ledger" style="margin:0;padding:0">
        ${ledger.map(e => html`
          <li key=${e.id}>
            ${e.id} · ${e.at.slice(11, 19)} · ${e.verdict}
            ${e.overridden && html`<span class="ov"> · operator override</span>`}
            <div style="color:var(--ink-faint)">${e.text}</div>
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

  const refresh = useCallback(async () => setSt(await api("/api/state")), []);
  useEffect(() => { refresh(); }, [refresh]);

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
  const doDraft = async (id) => {
    setDrafting(true);
    try { await api(`/api/cards/${id}/draft`, {}); await refresh(); }
    finally { setDrafting(false); }
  };
  const doSend = async (id) => { await api(`/api/cards/${id}/send`, {}); setActive(null); await refresh(); };
  const doDismiss = async (id) => { await api(`/api/cards/${id}/dismiss`, {}); setActive(null); await refresh(); };
  const reset = async () => { await api("/api/reset", {}); setActive(null); await refresh(); };

  return html`
    <div class="shell">
      <${Header} stats=${st.stats} lot=${st.lot} busy=${busy}
                 onReplay=${replay} onReset=${reset} />
      <div class="cols">
        <${ChatLog} log=${st.log} />
        <${Queue} queue=${st.queue} activeId=${activeId} onPick=${setActive} onSay=${say} />
        <${Draft} card=${card} busy=${drafting} onDraft=${doDraft}
                  onSend=${doSend} onDismiss=${doDismiss} ledger=${st.ledger} />
      </div>
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));
