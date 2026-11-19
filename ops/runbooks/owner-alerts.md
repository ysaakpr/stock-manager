# Runbook: the owner's Telegram alerts (OPS-ALERTS)

The owner reads alerts on Telegram, never logs. Every owner message is short plain English, IST
times, ₹ in lakh/crore, no internal ids, and ends with what (if anything) the owner should do. No
LLM cost, token or spend figure appears in any of them (owner decision, 16 Nov 2026). They go
through the platform alerter (`ALERT_PROVIDER`), and every one is deduplicated in
`alert_condition` (migration 0027), so a scheduler restart never repeats one.

| Alert | Where | When | Dedup key |
|---|---|---|---|
| Morning summary (jobs, M17 night, both momentum books vs NIFTY 50 TRI, M17 books vs control and bench, open alerts, disk; "All normal" / "Needs you") | `owner_morning_summary` job | 08:00 IST daily, shorter Sat/Sun | `owner:summary:<date>` |
| Paper book activity: buys/sells, into/out of the market, monthly rebalance, skipped (red data), book check failed | `owner_alerts` job | first 15-min tick after the 21:45 session | `owner:paper:<book>:<date>` |
| M17 nightly summary: per manager orders staged / refused, errors, ₹10 L book vs its control, bench | `owner_alerts` | first tick after the desk records the night | `owner:m17-night:<date>` |
| Ran degraded: backup model answered decision calls, filing digests not by the main model, past the data wait with gaps, paper session skipped | `owner_alerts` | with the nightly summary | `owner:degraded:<date>` |
| Loss limit: drawdown from the book's own peak > 10 %, again at each further 5 pp (2 paper books, 8 M17 manager books) | `owner_alerts` | onset per level; base level clearing = "back within limit" | `owner:loss:<book>:drawdown:<level>` |
| One-session drop > 4 % (same books) | `owner_alerts` | once per book and session | `owner:drop:<book>:<date>` |
| Stale data: NIFTY 50 / NIFTY 500 / Midcap 150 + Smallcap 250 TRI, India VIX, EOD prices, announcements, shareholding, board meetings more than 2 sessions behind | `owner_alerts` | onset and catch-up | `owner:stale:<dataset>` |
| Kill switch tripped (any account, any cause, by hand too) and reset — both CRITICAL | `owner_alerts` | onset and reset | `owner:kill:<account>` |
| Decisions waiting on you (`- [open] …` lines of `OWNER_PENDING_DECISIONS_FILE`) | `owner_weekly_decisions` | Monday 09:00 IST; nothing when none | `owner:decisions:<date>` |
| Server: memory > 90 % for 10 min, load > 6 for 15 min, postgres/app container not running, reboot | box guardian `~/campaign/watchdog/guardian.sh` | relayed by `ops_guardian` | `guardian:<key>` |

Thresholds are `Settings` fields (`OWNER_ALERT_*`, see `.env.example`); changing one is an `.env`
edit plus a scheduler restart. The Midcap/Smallcap TRI gap (stuck since 1 Oct) is listed in
`owner_alerts.KNOWN_GAPS`: it alerts once and says it is known.

## Checking without sending

```bash
uv run python -m dataplatform.owner_alerts preview   # prints today's morning summary, sends nothing
uv run python -m dataplatform.owner_alerts sample    # sends it once, titled "Sample morning summary"
```

"Sessions behind" counts NSE sessions after the newest date and before today (today's own data
lands in the evening), from the platform trading calendar. A news message is only sent for a
session recorded within `OWNER_ALERT_EVENT_MAX_AGE_HOURS` (12 h), so a deploy never replays old
nights. A part of a tick that fails is collected and fails the run (`failure_alerts` pages it);
the morning summary is still sent and names what it could not read.
