# Runbook — broker session re-authentication (AUTH_REQUIRED)

The broker API session must be logged in fresh **every trading day**. Indian brokers force a daily
logout of every API session, and re-opening it needs an interactive OAuth login plus 2FA — it cannot
be automated with a stored password (NSE consolidated NNF circular NSE/INVG/73992 §8.3.2.1.8; see
`ops/compliance/sebi-algo-memo.md` for the source register). So on any morning the login has not been
done, the daily loop wakes holding a **dead session**.

The auth interlock (task M5.15) is what stops the loop from trading on that dead session. It is the
exact counterpart, for lapsed auth, of the data-red interlock (`SKIPPED_DATA_RED`, §4.4) that stops
the loop trading on bad data. Both refuse to act on an invalid precondition.

## What the loop does while the session is dead

On a day the broker session is not authenticated, the loop does **not** place a single order.
Concretely, for that trading date it:

1. **Journals `AUTH_REQUIRED`** — a `SYSTEM` decision in `decision_journal`, the same shape as
   `SKIPPED_DATA_RED`, with no order attached and the re-auth instruction in its payload.
2. **Defers, never drops, the day's staged decisions.** Every decision the loop was about to stage
   is written as a `DEFERRED` journal entry naming its instrument (ISIN), side, quantity and sleeve,
   and is carried forward to the **next authenticated session** — where it is re-evaluated and staged
   normally. A decision that simply vanished because auth failed would be a journal lie; it does not
   happen here.
3. **Alerts you once — per outage, not per check.** The first dead-session day of a streak fires one
   re-authenticate alert. Subsequent dead days in the same streak do **not** re-alert (they are still
   journalled). The streak — and the "once" — resets the first day the session is valid again.

Paper mode is unaffected: `SimBroker` has no API token to lose, so its session is always valid and
the interlock never fires. This matters only for a real broker session (`KiteBroker`, M8).

## How to re-authenticate

The re-auth is a **human action** — the OAuth + 2FA login cannot be performed by the loop.

1. Open the broker's API login flow and sign in with 2FA to mint a fresh access token for the day.
2. Install the new token where the broker adapter reads it (the deployment's configured credential
   location — never commit it; `.env` and credentials are gitignored, CLAUDE.md/§Git).
3. Confirm the session is live before the next run: `Broker.session_valid()` must return `True`
   (equivalently, `execution.session.BrokerSessionGate(broker)(trading_date)` is truthy).

Once the session is valid again, the next daily run:

- journals no `AUTH_REQUIRED` (the streak is over, and a later outage will alert afresh);
- re-evaluates and stages the decisions that were `DEFERRED` while the session was down;
- proceeds through the normal data-red interlock and staging path.

## If it stays red

Each additional dead-session day keeps deferring that day's decisions and journalling
`AUTH_REQUIRED`, silently (no repeat alert). Nothing is lost, but nothing trades. If you cannot
restore the session — broker outage, revoked API access, expired subscription — treat it as a broker
availability incident: the platform is safely idle (zero orders, full journal), and the backlog of
deferred decisions will be re-evaluated against fresh data whenever the session returns, not replayed
blind.

## Where this lives in the code

| Piece | File |
|---|---|
| `session_valid()` on the broker seam, and `SessionExpired` | `execution/broker.py` |
| Read side — `BrokerSessionGate`, `SessionStatus`, the alert seam, `REAUTH_INSTRUCTION` | `execution/session.py` |
| Decision side — `AuthInterlock` (journals `AUTH_REQUIRED`/`DEFERRED`, dedupes the alert) | `analyst/monitor/interlock.py` |
| The `AUTH_REQUIRED` and `DEFERRED` decision kinds | `analyst/journal/models.py`, `dataplatform/store/migrations/0007_auth_required_decision.sql` |
| Tests | `tests/unit/test_auth_interlock.py` |
