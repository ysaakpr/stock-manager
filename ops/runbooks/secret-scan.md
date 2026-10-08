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
| CI, `secret-scan` job | `… --commits <base>..<head>` | every commit of the PR / push: its changed blobs **and** its message | the PR check |

The tool is detect-secrets, pinned in `uv.lock`, run through `ops/secret_scan.py`. It never touches
the network: live verification is stripped from its settings, whatever `.secrets.baseline` says.
Output names `path:line: detector` and **never prints the matched value**. Exit 0 clean, 1 finding,
2 misconfiguration (missing baseline, a required detector dropped from it, a git error).

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

Never: add an `exclude`/`should_exclude_file` filter or any blanket path rule (tests/, fixtures/,
docs/); re-enable the entropy detectors and bulk-accept what they find; remove a detector; or use an
inline `# pragma: allowlist secret` comment — the wrapper ignores it, by design.
`tests/unit/test_secret_scan.py` fails on the first three.

## Changing the detectors

- Detector set: `plugins_used` in `.secrets.baseline`. `ops/secret_scan.py` refuses to run (exit 2)
  without KeywordDetector, TokenAssignmentDetector, BasicAuthDetector, AWSKeyDetector and
  PrivateKeyDetector.
- Repo-local detectors: `ops/secret_scan_plugins.py` (token assignments, Anthropic keys). A new
  credential shape the stock set misses goes there, with a planted case in the test.
- The entropy detectors (Hex/Base64HighEntropyString) are off deliberately: this repo holds hundreds
  of sha256 content addresses, every one a "finding". See `ops/gates/M15.1-secret-scan-2026-10-08.md`.
- Upgrading detect-secrets: bump the pin in `pyproject.toml`, `uv lock`, then run the unit test and
  `make secret-scan`; a new detector can produce new findings on the existing tree.
