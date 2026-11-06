# Kite Connect read fixtures (M19, phase A)

Synthetic, shape-faithful response envelopes for the **read-only** owner-portfolio review
(`execution/kite_read.py`, `execution/kite_session.py`, `analyst/owner_portfolio/`). Field names
follow Kite Connect v3's documented payloads; every value is invented (INFY/TCS-style), and no file
holds real account data or a real credential. The suite never touches the network: an
`httpx.MockTransport` serves these bytes, and the snapshot tests assert they are written to disk
byte for byte.

| file | endpoint | purpose |
|---|---|---|
| `profile.json` | `GET /user/profile` | account behind the token (`user_id` `AB1234`) |
| `margins.json` | `GET /user/margins` | equity cash 125000.75; commodity disabled |
| `holdings.json` | `GET /portfolio/holdings` | INFY with 5 `t1_quantity` (held = 45), TCS with 2 pledged, NIFTYBEES and LIQUIDBEES (ETFs), an SME scrip the lake does not know, WIPRO sold out today (held 0) |
| `positions.json` | `GET /portfolio/positions` | today's WIPRO sale as a net position |
| `orders.json` | `GET /orders` | today's order book: one COMPLETE sell |
| `trades.json` | `GET /trades` | its fill |
| `mf_holdings.json` | `GET /mf/holdings` | one folio (fractional units) |
| `mf_sips.json` | `GET /mf/sips` | one monthly SIP |
| `mf_orders.json` | `GET /mf/orders` | one SIP instalment |
| `gtt_triggers.json` | `GET /gtt/triggers` | one single-leg stop GTT on TCS |
| `session_token.json` | `POST /session/token` | the login exchange's answer (synthetic token) |
| `error_token.json` | any | `TokenException` — expired or invalid session |
| `error_permission.json` | any | `PermissionException` — one endpoint refused, session alive |

Holdings rows carry `isin`, `quantity`, `t1_quantity`, `used_quantity`, `collateral_quantity`,
`average_price`, `last_price`, `close_price`, `pnl` and `mtf`, as Kite documents them.

`session_token.json` carries `"api_key": null`: Kite echoes the app key there, nothing here reads
it, and a null gives the secret scan's keyword detector nothing to match.
