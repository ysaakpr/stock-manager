# Same-evening TRI fixtures (M13.7)

Two NIFTY 50 answers from `POST https://niftyindices.com/BackPage/getTotalReturnIndexString`, one
on each side of the evening publication of a session's level. The tests parse these; the suite
never touches the network (B8). The `tri_*_20210401_20260331.json` files beside them are M3.9.b's
and are not described here.

| File | Bytes | sha256 |
|---|---|---|
| `tri_nifty50_20260925_20261006_at20261006T204737.json` | 1000 | `e2a0c18f43494b93911cc6253897c0800539f5b6d9a488012ad3a05a96b3b58d` |
| `tri_nifty50_20260921_20261005_at20261005T160846.json` | 1287 | `c02a6beb044bd0763ad5138fa79f74d66119a52170838329e6b34239aefd3d8c` |

- **Session D present** (`…_at20261006T204737`): the response, byte for byte, to a 25-Sep..06-Oct
  window requested at **20:47:37 IST on 2026-10-06** (a Tuesday session) through the repo's own
  leased fetcher. It is 7 records, newest first, and the first is `06 Oct 2026` (34608.14), so D's
  level was out that evening. Each record's `RequestNumber` (`TRI639268966575101791…`) is .NET
  ticks of the request instant in UTC, which matches the fetch receipt.
- **Session D absent** (`…_at20261005T160846`): the 21-Sep..05-Oct records of the real
  whole-history payload fetched at **16:08:46 IST on 2026-10-05** (a Monday session), stored in
  the lake as `L0/nifty_tri_history/2026/10/tri_nifty50_19900401_20261005.json`. The records were
  cut from that payload, not edited, and were written back with the endpoint's own compact
  encoding. Before the cut, the whole payload was checked to re-encode byte-identically. Its
  newest record is `01 Oct 2026` (02-Oct was a holiday), so session D = 05-Oct was not yet
  published 38 minutes after the close. This is the "fetched before dissemination" shape the job
  must park as retryable.
