# Runbook — the secret scan (M15.1)

**This repo is public.** A credential in any pushed commit is published the moment it lands, and
deleting it in a later commit un-publishes nothing — the only remedy is rotation
(`ops/runbooks/secret-leak.md`). The secret scan is the gate in front of that: invariant #13
(AGENTIC_CONTEXT §6), made mandatory by HUMAN_DECISIONS D5 before any Kite credential exists here.

## What runs where

| Where | Command | Scans | Blocks |
|---|---|---|---|
| `make check` (first step) | `uv run python ops/secret_scan.py` | every tracked + untracked-not-ignored file | the gate |
| pre-commit hook | `… --staged` | the staged blobs — what the commit will contain | the commit |
| commit-msg hook | `… --message <file>` | the commit message | the commit |
| CI, `.github/workflows/secret-scan.yml` (never cancelled by a newer push) | `… --commits <base>..<head>` | every commit of the PR / push: its changed blobs **and** its message | the PR check |

The tool is detect-secrets, pinned in `uv.lock`, run through `ops/secret_scan.py`. It never touches
the network: live verification is stripped from its settings, whatever `.secrets.baseline` says.
Output names `path:line: detector` and **never prints the matched value**. Exit 0 clean, 1 finding,
2 when the scan cannot be trusted: a file it cannot read, a corrupt baseline, a detector missing or
failing to load, a filter that would skip files, a `--commits` argument that is not `A..B`.

Every file is read by the wrapper, not by detect-secrets (which skips unreadable and non-UTF-8 files
without a word). Non-UTF-8 text is decoded (UTF-16 by BOM, else UTF-8, else latin-1) and scanned.
Every line of a text file is scanned as written, comments included; detect-secrets' parsed views
(YAML/INI) are an extra pass, never a replacement. A file is read as UTF-16 when it starts with a
BOM, or — without one — when **every** NUL byte sits on the same parity **and** NULs fill at least a
quarter of that parity's positions (about one byte in eight overall); that is ASCII-heavy UTF-16LE
or BE.

**Known gaps — what the scan cannot see, by design and named:**

- **Binaries — and UTF-16 that does not look like UTF-16.** Any other file containing a NUL byte is
  treated as binary and not scanned: zip, xlsx and most pdf, but also BOM-less UTF-16 whose NULs
  are too sparse (mostly non-Latin text, where few high bytes are zero) or fall on both parities
  (a character such as U+0100 puts a NUL on the other side). Each run prints them under
  `NOT SCANNED, binary (N)` (60 tracked fixtures today); nothing passes silently, but nothing
  inside them is checked. Never commit a credential inside an archive or office file, and save
  text as UTF-8.
- **A value split across lines.** Detection is line-based: a multi-line parenthesised call whose
  value sits alone on its own line, with no keyword, no known prefix and no Kite shape, is missed.
- **A bare token outside the Kite-shape scope.** A value with no keyword, no known prefix and no
  `user:pass@` is caught only if it is exactly 32 mixed-case alphanumerics (upper, lower and digit;
  not pure hex) **and** either sits on a line mentioning kite/token/access or is in a
  .env/.json/.yaml/.yml/.md/.toml file. Such a value in, say, a .py/.sh/.txt/.csv file on a line
  without those words, or a bare credential of any other shape (another length, pure hex, with
  symbols) anywhere, is missed: the entropy detectors are off (see the gate note).
- **A keyword-named value shorter than 16 characters, or without both a letter and a digit**, is
  left to detect-secrets' stock KeywordDetector, which needs quotes in code files.
- **An unquoted value that looks like a reference** — a dotted chain (`settings.db2_password`), a
  call or a subscript — is treated as code, not as a credential. A real secret written unquoted in
  that shape (e.g. `token = abc.def123…` in a .env file) is missed.
- **A credential under a name with no keyword** (`KITE_SESSION=<value>`, `auth: <value>`) is
  caught only if it has a known prefix, a `user:pass@`, or the Kite shape in scope.

`make secret-scan` runs the working-tree scan on its own (~40 s for the whole tree on this box).

## Install the hooks (once per clone)

```bash
make hooks          # git config core.hooksPath ops/hooks
```

`core.hooksPath` lives in the clone's shared config, so it covers every worktree of the clone; each
worktree runs the hooks from its own checkout of `ops/hooks/`. A worktree on a branch that predates
M15.1 has no `ops/hooks/` and therefore no hook — `make check` and CI still scan it. Git never runs a
repo's hooks on its own, so a fresh clone has none until `make hooks` is run; the hooks are the
early warning, `make check` and CI are the controls that cannot be skipped.

To check the hooks are live: `git config --get core.hooksPath` prints `ops/hooks`.

**Never `git commit --no-verify` past a finding.** If the hook is wrong, it is a false positive —
handle it below, in a reviewed diff.

## On a finding

1. **Assume it is real** unless you can show otherwise in under a minute (secret-leak.md §0).
2. **Not yet pushed** (hook or `make check` caught it): take the value out of the file — read it
   from the environment / `.env` through a `SecretStr` setting instead. If the value is a live
   credential, **rotate it anyway**: it has been on disk in a working tree, possibly in an editor
   swap file, a shell history, a tool log. Then commit.
   - A commit that already exists locally but is unpushed: a new commit that removes it is **not**
     enough, because CI scans every commit and the push would publish the old one. History is not
     rewritten here (§3 human-only) — stop and ask the owner, after rotating.
3. **Already pushed** (CI caught it, or it was found later): it is published. **Rotate first** —
   `ops/runbooks/secret-leak.md` §1 — then remove it in a normal commit. **Never just delete it**:
   the blob stays reachable on GitHub, in every clone and in the events API; deletion without
   rotation is the same as doing nothing.
4. An agent that hits a finding it cannot prove fake: park the task and report the file, line and
   commit to the human. Do not try to clean history, and never paste the value into a report.

## Adding a false positive

The only allowlist is `.secrets.baseline`. Each entry is keyed on **path + detector + hash of the
matched value**: it accepts that one value in that one file, and nothing else — the same value in
another file, or a new value in the same file, is still a finding. Line numbers are recorded but not
compared, so editing above an accepted line does not resurrect it.

1. Look at every reported line and be sure each is not a credential: a test fake, the dev-default
   DSN, a detector's own source. If it *could* authenticate anywhere, it is not a false positive.
2. Prefer changing the line so it no longer matches (build a test fake at runtime from fragments,
   as `tests/unit/test_secret_scan.py` does). That keeps the baseline short.
3. Otherwise accept exactly the reviewed file(s):

   ```bash
   uv run python ops/secret_scan.py --accept path/to/file
   ```

   This appends only that file's current findings and leaves every other entry untouched. Do
   **not** use `detect-secrets scan --baseline …` for this: with explicit files it drops every
   other file's entries.
4. `git diff .secrets.baseline` must show only the entries you reviewed. Say in the commit message
   what each one is and why it cannot authenticate.
5. **Do not prune an entry just because the line left the working tree.** CI's `--commits` scan
   reads every commit of the PR, so an accepted value that still exists in any commit of an open
   branch must keep its entry, or that PR's check goes red. Prune only once no unmerged commit
   carries the value (`uv run python ops/secret_scan.py --commits origin/main..HEAD` before pushing
   tells you).

Never add a filter, re-enable the entropy detectors, remove a detector, or rely on an inline
`# pragma: allowlist secret` comment. What actually stops each:

| Attempt | Stopped by |
|---|---|
| any filter beyond the value heuristics (`should_exclude_file/line/secret`, a regex, a wordlist, a `file://` filter) | `ops/secret_scan.py` exits 2 |
| a `keyword_exclude` on KeywordDetector | exits 2 |
| re-listing a skip filter (swagger, lock-file, indirect-reference, likely-id-string, line-allowlist, verification) | silently stripped — it never takes effect |
| removing any of the 30 detectors, or a detector that fails to load | exits 2 |
| an inline `pragma: allowlist secret` | ignored |
| re-enabling Hex/Base64HighEntropyString | not rejected by the scanner; `test_baseline_holds_only_hashes_and_reviewed_entries` fails |
| bulk-accepting real findings with `--accept` | **nothing but review** — read the baseline diff |

## Changing the detectors

- Detector set: `plugins_used` in `.secrets.baseline`, which must list every name in
  `REQUIRED_PLUGINS` in `ops/secret_scan.py` (30: detect-secrets' own minus the two entropy ones,
  plus the repo-local five). Each must load from the file its entry names, or the scan exits 2.
- Repo-local detectors: `ops/secret_scan_plugins.py` — assignments (plain, `:`-style, or
  Python-annotated with an optional `SecretStr(…)`/`Secret(…)`/`str(…)` wrapper) to any name
  containing token, secret, api-key/api_key/apikey, password, passwd or pass, joined by `_` or `-`;
  `…token("…")` calls,
  `Authorization: token|Bearer` headers, Kite-shaped bare tokens, conninfo `password=`, Anthropic
  keys. A new credential shape the stock set misses goes there, in `REQUIRED_PLUGINS`, and gets a
  planted case in the test.
- The entropy detectors (Hex/Base64HighEntropyString) are off deliberately: this repo holds hundreds
  of sha256 content addresses, every one a "finding". See `ops/gates/M15.1-secret-scan-2026-10-08.md`.
- Upgrading detect-secrets: bump the pin in `pyproject.toml`, `uv lock`, then run the unit test and
  `make secret-scan`; a new detector can produce new findings on the existing tree.
