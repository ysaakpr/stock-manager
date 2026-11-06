# Kite integration, phase A — an agentic, read-only review of the owner's self-managed portfolio

**Status:** APPROVED for build (owner, 2026-11-06, chat; decisions in §6). M19.6 deferred.
**Scope:** phase A = read the owner's real Zerodha account over Kite Connect and give an agent (a Claude
Code session on this box) typed, lake-enriched tools to review it. **No order is placed, modified or
cancelled by anything in phase A.** Phase B (real-money portfolio management through `KiteBroker`,
M8.3/M8.4) comes later and reuses the session and transport defined here.

## 1. What Zerodha actually offers (verified 2026-11-06)

| Fact | Consequence for us | Source |
|---|---|---|
| **Personal app is free**: "full order, GTT and alerts management; margin computation and portfolio management; **no live market data or historical data**." No WebSocket. | Phase A uses only portfolio / funds / order-book reads. Prices and history come from **our lake**, not Kite (the holdings payload's own `last_price`/`close_price` is a convenience mark only). | Zerodha support, Kite Connect API FAQs |
| **Every access token comes from a human login.** Login URL → Kite login + 2FA → redirect carries a `request_token` (lives minutes) → `POST /session/token` with `checksum = sha256(api_key + request_token + api_secret)` → `access_token`. | A daily, human-initiated login. Nothing in this repo stores a password or TOTP or automates the 2FA (NSE/INVG/73992 §8.3.2.1.8, `ops/compliance/sebi-algo-memo.md`). | kite.trade/docs/connect/v3/user |
| Token **expires 06:00 IST next day**, or on a master logout from Kite web. `refresh_token` exists only for approved platforms. | The session store records expiry; tools report "login needed" instead of failing obscurely. | same |
| A token carries **no scopes** — the same token that reads holdings can place orders. | Read-only must be enforced **by our code, structurally** (§4 I1), not assumed from the token. | same |
| Static IP is required **only to place orders** (since 1 Apr 2025/2026); "all other endpoints … remain accessible from any IP". | Phase A needs no static IP. Phase B does. | support FAQ; sebi-algo-memo |
| Kite Connect has **no reports API** (tradebook history, P&L, tax P&L, ledger are Console downloads). `/trades` and `/orders` are **today only**. | Lot dates / realised P&L / LTCG-STCG need the Console exports (optional import, §6 M19.6). The daily snapshot is how we build our own history. | review of 2026-11-06 (`execution/kite_broker.py` G10) |
| Holdings rows carry `isin`, `quantity`, `t1_quantity`, `used_quantity`, `collateral_quantity`, `average_price`, `last_price`, `close_price`, `pnl`, `mtf{}`. | ISIN is read straight from the row (invariant #2). Held quantity = `quantity + t1_quantity` (the G7 lesson). | kite.trade/docs/connect/v3/portfolio |

**"Personal token mode".** The public docs show no token minted directly in the developer console;
every documented route is the login → `request_token` → exchange flow above. The session store (M19.1)
therefore accepts **either**: (a) the exchange flow, or (b) a pasted `access_token`, if the
developers.kite.trade console for a Personal app shows one. Both yield the same
`Authorization: token <api_key>:<access_token>` header, so nothing downstream cares which. **Owner to
confirm which the console shows.**

## 2. Shape

```
 owner (browser, once a day)                     Claude Code session on this box
   │ 1. kite login → 2FA                            │  "review my portfolio"
   ▼                                                ▼
 kite-session CLI ──writes──► ~/.config/smagent/   MCP server "kite-portfolio" (stdio, local)
 (M19.1)                      kite-session.json      │  tools: session_status, refresh, overview,
                              (0600, outside repo)   │  holding_detail, exposures, changes, …
                                     │               ▼
                                     └──read──► KiteReader (M19.2) ── GET-only allowlist ──► api.kite.trade
                                                     │
                                                     ▼
                                     data/private/kite/<date>/… raw snapshot (immutable, gitignored)
                                                     │
                                                     ▼
                                     OwnerPortfolio (typed, ISIN-keyed) ⨝ lake (prices, PIT fundamentals,
                                     sector, liquidity, thin-trading label)  (M19.3/M19.4)
```

The agent is an ordinary Claude Code session; the "integration" is the MCP server it calls. The CLI
exposes the same functions, so everything also works without MCP (and is how the tests drive it).

## 3. Components (proposed task split — milestone M19)

**M19.1 · Kite session (shared with phase B).** `execution/kite_session.py`.
- `kite-session login` prints the login URL (`https://kite.zerodha.com/connect/login?v=3&api_key=…`); the
  owner logs in and pastes back the **redirect URL** (or just the `request_token`). The redirect URL
  registered on the Personal app can be `http://127.0.0.1/kite/callback` — the browser shows an error
  page, the token is in the address bar; no public endpoint is needed. (A console callback route under
  `https://sm.<domain>/console/kite/callback` is a later convenience, NEEDS_GO, because it is a new
  internet-facing write path.)
- `kite-session set-token` for the pasted-token route (read from stdin, never argv).
- `kite-session status` → logged in?, masked user id, expires-at (IST), age of last snapshot.
- Store: `~/.config/smagent/kite-session.json`, mode 0600, `{access_token, user_id, login_time,
  expires_at}`; refuses to load a file with wider permissions. Loaded as `SecretStr`, unwrapped only in
  the header builder. Settings gain `kite_session_file` (path, not a secret); `KITE_API_KEY` /
  `KITE_API_SECRET` already exist in `Settings` as `SecretStr`.
- No logout call: Kite's logout puts the access token in a query string, which our secrets rule
  forbids; the token dies at 06:00 IST anyway, and `kite-session forget` deletes the file.

**M19.2 · Read-only Kite client.** `execution/kite_read.py`.
- `KiteReadTransport` has **one** network method, `get(path, params)`, and a fixed allowlist:
  `/user/profile`, `/user/margins`, `/portfolio/holdings`, `/portfolio/positions`, `/orders`,
  `/trades`, `/mf/holdings`, `/mf/sips`, `/mf/orders`, `/gtt/triggers`. Anything else raises before a
  socket opens. It shares `_unwrap` (Decimal parsing, `TokenException` → session error) with
  `LiveKiteTransport`, and additionally maps httpx timeouts/connection errors to typed errors (closes
  G11 for reads) and retries a 429/5xx with bounded backoff.
- `KiteReader` returns typed, frozen models: `KiteHolding` (isin, symbol, exchange, quantity,
  t1_quantity, used_quantity, collateral_quantity, average_price, last_price, close_price, kite_pnl),
  `KitePosition`, `KiteFunds`, `KiteOrder`/`KiteTrade` (today), `KiteMfHolding`, `KiteGtt`. Money is
  `Decimal`; a missing field is `KiteMalformedResponseError`, never a silent zero.
- Does not import `execution.kite_broker` beyond the shared envelope parser, and nothing in phase A
  imports `KiteBroker`.

**M19.3 · Snapshots.** `analyst/owner_portfolio/snapshot.py`.
- `refresh()` reads every allowlisted endpoint once and writes the raw envelopes, byte for byte, to
  `data/private/kite/<YYYY-MM-DD>/<HHMMSS>/<endpoint>.json` plus a `manifest.json` (sha256 per file,
  clock time IST). Write-once, never edited — the same discipline as L0, but **outside** `data/L0` so it
  is not part of lake rebuilds or the S3 L0 mirror (it is the owner's private account, not market data).
- `load(snapshot_id)` parses a snapshot into `OwnerPortfolio`; every review tool works from a snapshot,
  never from a live call, so a review is reproducible and the agent can't hammer the API.
- Minimum refresh interval (default 5 min); a refresh during an expired session says "login needed".

**M19.4 · Review context (the lake join).** `analyst/owner_portfolio/context.py`.
- Per holding, keyed by ISIN through the identity master: latest lake close and session; 1m/3m/6m/1y
  return (adjusted series); distance to 50/200-dma and 52-week high/low; median traded value
  (liquidity); thin-trading label (M13.8); sector/industry; index membership; latest PIT fundamentals
  (TTM sales/profit growth, margins, P/E, P/B where the lake has them) with their filing date; pending
  corporate actions. Reuse the per-stock reads M17's dossier builder uses — **read them, don't change
  them** (M17 inputs are frozen until S0, 13 Nov).
- Portfolio level: value and weight per holding (lake close × held qty, with Kite's mark alongside),
  unrealised P&L on Kite's average cost, cash and liquid-ETF share, concentration (top-5, HHI), sector
  and size-bucket weights, ETF vs direct equity, holdings the lake cannot price (named, not dropped).
- A holding with no lake identity (e.g. an SME scrip or an unlisted bond) is listed with Kite's own
  numbers and an explicit `lake_context: unavailable(<reason>)`.

**M19.5 · Agent interface — MCP server.** `analyst/owner_portfolio/mcp_server.py`, stdio, registered in
the project `.mcp.json` as `kite-portfolio`. Tools (all read-only):

| Tool | Returns |
|---|---|
| `session_status()` | logged in?, expires-at IST, last snapshot id/time |
| `refresh()` | new snapshot id (or "login needed: run `kite-session login`") |
| `portfolio_overview(snapshot?)` | totals, cash, holdings table with lake context summary |
| `holding_detail(isin_or_symbol)` | full Kite row + full lake context for one holding |
| `exposures(snapshot?)` | sector / size / ETF / concentration breakdowns |
| `changes(since)` | holdings diff between two snapshots (bought, sold, qty changed) |
| `orders_today()` / `trades_today()` | today's order book and fills |
| `mf_holdings()` / `gtt_triggers()` | MF folios; standing GTTs |

Symbols are accepted as **input convenience** and resolved to ISIN at the edge; every output is keyed by
ISIN with the symbol as a display label. Adds one new dependency, the `mcp` Python SDK (owner-approved
2026-11-06; the first MCP server in the repo). The same tools are also a CLI,
`uv run python -m analyst.owner_portfolio <tool>`, emitting JSON/markdown.

**M19.6 · Console exports import (optional, later).** Parse the Console Tradebook / Tax-P&L / Ledger
exports the owner already downloads (xlsx/CSV) into the same private area, to give lot dates,
holding-period (LTCG vs STCG), realised P&L and the FY tax picture. This is the only route to those
numbers (§1). Not needed for a first useful review.

## 4. Invariants (each gets a test that fails if it is broken)

- **I1 · Read-only by construction.** No phase-A module can send a non-GET or a non-allowlisted path:
  `KiteReadTransport` has no write method; an AST test asserts no `post/put/delete/request(` call and no
  `KiteBroker` import anywhere under `analyst/owner_portfolio/` and `execution/kite_read.py`; the MCP
  server's tool list is pinned by a test. The agent can recommend; it cannot trade.
- **I2 · The owner's portfolio is private.** Snapshots live only under gitignored `data/private/`;
  never in Postgres, the decision journal, the status API/console, or a committed file. Logs carry
  counts and snapshot ids, never symbols, quantities or values. M17 cannot see it (import-boundary test:
  `analyst.fundmanager` never imports `analyst.owner_portfolio`). Note: tool results do reach the model
  provider as conversation content — that is the point of an agentic review, and the owner opts in each
  time they ask.
- **I3 · Secrets.** API key/secret and access token are `SecretStr`, token file 0600 outside the repo,
  never in argv, URL, log, snapshot or exception text; the secret scan covers the new fixtures. No
  stored password/TOTP, no automated login.
- **I4 · House rules.** `Decimal` money, ISIN joins, injected `Clock`, IST timestamps, fail loud
  (an expired session or malformed payload is a typed error the tool reports, not an empty portfolio).
- **I5 · No interference.** No scheduler job, no change to M17 inputs or the paper desk; tests are
  offline with recorded fixtures (`tests/fixtures/kite_read/`, synthetic values, shape-faithful).

## 5. What phase B (real money) will reuse, and what it still needs

Reuses: the session store and login CLI (fixes G3), the hardened transport (G11), `KiteHolding` with
`t1_quantity` (G7), the snapshot discipline for daily broker capture, `/trades` capture for fills.
Still needs, unchanged from `ops/runbooks/live-orders.md`: static IP, rate/daily caps (G1), market
protection (G2), execution driver (G9), ledger from Console exports instead of the fictional `/ledger`
(G10), recon scoped to the strategy's ISINs if the account is shared with the self-managed portfolio,
`amo` variety for after-close placement, and the M8.3/M8.4 human gates. The decision to run the
strategy in the **same** Zerodha account as the self-managed portfolio or a separate one is the
owner's, and should be made before phase B.

## 6. Owner decisions (2026-11-06)

1. **Token route — login exchange.** Creating the Personal app gives an API key + API secret only; the
   app asks for a redirect URL and a postback URL. So M19.1 builds the login → paste-redirect-URL →
   `/session/token` exchange. `set-token` stays as a fallback but is not the primary route.
2. **App registration values.** Redirect URL: `http://127.0.0.1/kite/callback` (paste flow; nothing has
   to listen there). Postback URL: **leave empty** — it is optional, and it is where Kite POSTs order
   updates, which phase A never creates. Phase B will need an HTTPS endpoint behind Caddy that verifies
   Kite's checksum; that is decided with phase B.
3. **Interface — MCP server**, with the `mcp` Python SDK as a new dependency (approved), and the CLI
   underneath.
4. **Build now**, in a sub-agent, outside 21:30–00:30 IST. M19.6 (Console exports) later.
