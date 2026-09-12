# SideStage

A real-time seller copilot for **eBay Live** trading-card sellers.

It reads the live chat stream, surfaces only what is worth answering, drafts replies
grounded in the listing, catalog and policy record — and **blocks anything it cannot
prove** before it reaches a buyer.

> A viewer asks "is that 1st edition?" about a Champion's Path Charizard. The model says
> yes. The verifier blocks it, because the set catalog records that no English card in
> that set was ever printed with a 1st Edition stamp. That is a $2,000 mistake caught by
> domain logic, not by a tone filter.

**Status: in development.** The scaffold, configuration and data model are in place. The
pipeline, console, and evaluation harness are being built — this README grows with them.

---

## Run

```bash
uv sync                                    # Python 3.13, resolved from uv.lock
uv run uvicorn app.main:app --reload       # → http://127.0.0.1:8000
```

No credential is required. With `ANTHROPIC_API_KEY` unset the app runs in **replay mode**
against recorded fixtures and the full workflow still works, deterministically. Set a key
in `.env` (copy `.env.example`) to run against live models.

`GET /healthz` reports which mode you are in, the models in use, and the latency budgets —
without ever reporting the key.

## Test

```bash
uv run pytest
```

---

## Documentation

| | |
|---|---|
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Every architectural decision, the alternative rejected, and why. The spine of the TDD. |
| [`docs/DOMAIN_PRIMER.md`](docs/DOMAIN_PRIMER.md) | How trading cards work, and what makes a claim about one verifiable. §7 is the verifier registry. |
| [`docs/research/`](docs/research/) | Field observation instrument and notes. |

## Layout

```
app/
  config.py      runtime settings, latency budgets, domain thresholds
  models.py      the vocabulary — a CLAIM cites a FACT; start here
  main.py        FastAPI: REST, operator WebSocket, static console
data/            seeded catalog, set records, comps, policies (modeled)
static/          the operator console — no build step (see DECISIONS.md D-07)
fixtures/        recorded LLM responses for replay mode
```

## A note on the data

The seeded catalog is **modeled from public collector references**. It is not licensed
eBay or TCGplayer data, and the marketplace adapter is a mock with deliberately realistic
failure semantics rather than a live integration. Both are stated plainly in the TDD's
known limitations.
