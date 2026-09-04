# Runbook — a secret reached the repo, a log, or an artifact

**This repo is public** (`github.com/ysaakpr/stock-manager`). Assume a published secret was scraped
within minutes of the push. Bots watch the GitHub events firehose for exactly this; the window between
push and exploitation is measured in seconds for cloud keys, not hours.

The whole runbook is one idea: **rotate first, clean up second, argue about how it happened third.**

---

## 0. Decide if it is actually a secret

Not everything that trips a scanner is live. A secret is anything that can *authenticate*: an API key,
token, password, private key, session cookie, or a connection string with a password in it. A fake in a
test (`SecretStr("t")`, `123456:test-token`), a placeholder in `.env.example`, and the dev-default DSN
`postgresql://trading:trading@localhost:5433/trading` are not secrets.

If you cannot tell in under a minute, **treat it as live** and rotate. Rotating a key that turned out to
be fake costs a few minutes. Not rotating one that was real costs the account.

---

## 1. Rotate at the provider — before anything else

Do this before you look at git, before you write the incident note, before you tell anyone.

| Credential | Where to rotate | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys → revoke, then create | Revoke the old key explicitly; creating a new one does not disable the old one. Check usage/billing for calls you did not make. |
| `KITE_API_KEY` / `KITE_API_SECRET` | Kite Connect developer console → regenerate | **Highest severity in this repo — this one can move real money.** Regenerate the secret, then check the order book and positions for anything you did not place. If anything is unexplained, trip the kill switch (X1) before you continue. |
| `KITE_ACCESS_TOKEN` | Re-run the login flow | Expires daily anyway, but invalidate the session now rather than waiting. |
| `ALERT_SMTP_PASSWORD` | Mail provider → app password → revoke and reissue | Check the sent-mail folder; a stolen SMTP credential is used to send, and that burns the sending domain. |
| `ALERT_TELEGRAM_BOT_TOKEN` | BotFather → `/revoke` → `/token` | The token *is* the whole credential — there is no second factor. |
| Postgres password | `ALTER ROLE ... PASSWORD ...`, then update `.env` / `ops/.env` | Only urgent if the DB is reachable off-host. Check `ops/docker-compose.yml` — publishing on `0.0.0.0` makes it urgent. |

**Revoke, do not just rotate.** Issuing a new key while the old one still works fixes nothing.

---

## 2. Contain

- Confirm the new credential is in `.env` / `ops/.env` (untracked) and **not** in the commit you are
  about to make.
- If the leak was a broker credential and anything in the order book is unexplained, trip the kill
  switch and stop the daily loop before investigating.
- If the secret reached the **decision journal**, it is permanent — the journal is append-only
  (AGENTIC_CONTEXT §6 #12) and must not be rewritten to hide it. Rotation is the only remedy, which is
  precisely why invariant #13 forbids secrets in the journal in the first place.

---

## 3. Stop the bleeding in the repo

Remove the secret from the working tree and commit that normally. This makes the current checkout clean.

**It does not unpublish anything.** The blob is still reachable by SHA on GitHub, still in every clone
and fork, and still in the events API. That is fine — you already rotated in step 1, so the published
value is dead.

**Do not rewrite history on your own initiative.** `git filter-repo` / force-push is a **§3 human-only
decision**: it breaks every clone, does not remove the blob from GitHub's servers without a support
request, and is usually not worth it for a credential that has already been revoked. Park it and ask.

An agent that finds a leaked secret: rotate is not yours either — you cannot log into a provider console
(§3.7). **Park the task, report the exact file and commit, and escalate to the human immediately.** Do
not attempt cleanup, do not amend, do not force-push.

---

## 4. Close the hole that let it through

A leak is a control failure, not a typing mistake. Before closing:

- Which gate should have caught it? Add the pattern to the secret scan in `make check`.
- Was it a *code path* rather than a literal — a secret in a log line, a URL, a traceback's frame
  locals, an exception message, a recorded fixture? Fix the code path, and add a test that fails if the
  secret becomes loggable again. The masking test beside `tests/unit/test_config.py` is the model:
  assert the `repr` does not contain the value.
- Add a line to `ops/BACKLOG.md` if the fix is bigger than the incident.

---

## 5. Write it down

Append to this file, below the line, one entry per incident: date, which credential, how it got in, how
long it was public, what was rotated, what evidence of misuse was found, and which gate now catches it.
Facts only — this is a maintenance record, not a confession.

---

## Incidents

*None. The 2026-08-10 audit of the working tree and all 228 blobs in history found no secret ever
committed; `.env` was never tracked.*
