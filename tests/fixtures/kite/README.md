# Kite Connect recorded fixtures (M8.1)

Recorded/synthetic responses that mirror the payload shapes Kite Connect's REST API documents,
used to test `execution/kite_broker.py` entirely offline (no credential exists — B4 — and the test
suite never touches the network).

Each file is a full Kite response **envelope** (`{"status": ..., "data": ...}` or
`{"status": "error", "error_type": ..., "message": ...}`), exactly as the API returns it. The test
transport unwraps them with the same `LiveKiteTransport._unwrap` the production transport uses, so
the fixtures exercise the real envelope-parsing and error-mapping path — money parsed as `Decimal`
via `parse_float=Decimal`, `TokenException` mapped to a session error.

| file | endpoint | purpose |
|---|---|---|
| `profile.json` | `GET /user/profile` | a live session (`session_valid` → True) |
| `holdings.json` | `GET /portfolio/holdings` | settled holdings, ISIN carried in the row |
| `positions.json` | `GET /portfolio/positions` | net positions (one open, one squared-off) |
| `margins.json` | `GET /user/margins` | equity available/utilised |
| `ledger.json` | Console ledger report | cash ledger rows (a cash row and a scrip row) |
| `order_place.json` | `POST /orders/regular` | placement acknowledgement (`order_id`) |
| `order_modify.json` | `PUT /orders/regular/:id` | modify acknowledgement |
| `order_cancel.json` | `DELETE /orders/regular/:id` | cancel acknowledgement |
| `order_open.json` | `GET /orders/:id` | order history, latest state OPEN (modifiable) |
| `order_complete.json` | `GET /orders/:id` | order history, latest state COMPLETE (terminal) |
| `error_token.json` | any | `TokenException` — expired daily session |
| `error_unknown_order.json` | `GET /orders/:id` | lookup of an id Kite never issued |

Values are synthetic but shape-faithful. Instruments: INFY (`INE009A01021`) and TCS
(`INE467B01029`), NSE.
