# L0 S3 mirror (SSE-C)

`data/L0` is copied to S3 with an SSE-C customer key, by `ops/l0-s3-sync.sh`. It is a **copy**:
the pipeline still reads and writes local `data/L0`, which stays the authoritative record until
the storage layer learns to write to S3 itself (the AWS move). It is **run by hand** — there is no
scheduler job for it.

L0 is immutable (invariant #1), so the mirror only grows and never changes: `sync` lists the keys
already under the prefix and uploads only names S3 does not have. It never deletes, and it never
re-uploads an existing key — not even one whose size or mtime differs. A local file that no longer
matches its mirrored copy is an incident for the owner (AGENTIC_CONTEXT §3.10), which `verify`
surfaces; it is not drift for a sync to paper over.

## What lives where (none of it in this repo)

| What | Where | Mode |
|---|---|---|
| Bucket and prefix (`L0_S3_BUCKET`, `L0_S3_PREFIX`) | `~/.config/smagent/l0-s3.env` | 600, yours |
| SSE-C key, 32 bytes, base64 | `~/.config/smagent/sse-c/sm-raw.key.b64` | 400 or 600, yours |
| Key fingerprint (sha256 of the raw bytes, first 16 hex) — required | `~/.config/smagent/sse-c/sm-raw.key.fingerprint` | 644 |

The script refuses (exit 2) an env file or key file not owned by you or with any group/other bit,
**before** it sources the env file (a writable one would be code execution as you) or reads the
key. It refuses a missing fingerprint too.

The bucket name carries the AWS account id and the repo is public, so it stays in the untracked
env file, and the script prints only the prefix. **The key is the only way to read the mirror.**
Lose it and every object is unreadable; keep a second copy off this machine (the base64 text is
one line, made for a password manager).

## Bucket requirements

`sync` and `probe` refuse (exit 2) unless `get-bucket-versioning` reports `Enabled` or Object Lock
is configured. The script never overwrites, but versioning is what keeps the original bytes if
anything else ever does — a hand-run `aws s3 cp`, a future writer, a mistake.

The writing role should also be unable to delete. Attach an explicit deny to the instance role
(a deny beats any allow):

```json
{
  "Effect": "Deny",
  "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketVersioning",
             "s3:PutLifecycleConfiguration"],
  "Resource": ["arn:aws:s3:::<bucket>", "arn:aws:s3:::<bucket>/<prefix>/*"]
}
```

What the role needs: `s3:ListBucket`, `s3:GetBucketVersioning`, `s3:GetBucketObjectLockConfiguration`,
`s3:GetObject`, `s3:PutObject`. Both are owner changes to the AWS account, made in the console or
by the owner — not by this script.

## Use

```bash
ops/l0-s3-sync.sh probe          # key decodes, is 32 bytes, matches its fingerprint, and S3's
                                 # SSECustomerKeyMD5 for an object from the listing equals the key's
ops/l0-s3-sync.sh sync           # upload the settled files whose keys S3 is missing
ops/l0-s3-sync.sh verify 300     # keys + sizes match exactly; 300 random objects match by sha256
```

From a worktree, set `DATA_ROOT=/home/ubuntu/stock-manager/data` — the worktree has no lake.
A full listing compare over ~224k objects takes several minutes even when nothing is new. The
first sync into an empty prefix is one `aws s3 cp` per file and is slow; later syncs upload only
what is new.

A file modified in the last 10 minutes (`L0_S3_SETTLE_MINUTES`, the same margin as `l0_backup`)
is **deferred** to the next run, since an uploaded key is frozen and a half-written payload would
stay half-written in the mirror. `sync` reports `uploaded`, `failed` and `deferred`; `verify`
compares settled files only.

Exit codes: 0 ok · 1 sync had failed uploads, verify failed, or probe could not confirm the key ·
2 config/key/permissions invalid or bucket not versioned · 3 another sync running · 4 no `aws`
(or no `openssl`, for `probe`).

The lock is `~/.local/state/smagent/l0-s3-sync.lock`, one path for every caller.

## What `verify` proves

`verify` downloads a sample because under SSE-C the ETag is not the content MD5: a listing proves
names and sizes, not bytes. Each sampled object's sha256 is compared with the local file and, when
`~/backups/l0/MANIFEST.sha256` exists (`L0_MANIFEST`), with the hash `l0_backup` recorded — so a
local file that changed after it was mirrored shows up as a disagreement rather than a match. A
sampled download that fails is counted, not fatal: the summary line always prints and the exit
code is 1. A `missing` count right after a sync is normally files an L0 writer (the scheduler, a
campaign) created mid-sync or that were still settling — run `sync` again and re-verify.

## How the key is handled

Decoded only into a pipe and given to the AWS CLI as `fileb:///dev/fd/N`: never written to disk
decoded, never on a command line, never logged. Do the same in any ad-hoc command:

```bash
aws s3 cp s3://$L0_S3_BUCKET/sm-raw/<path> out --sse-c AES256 \
  --sse-c-key fileb://<(base64 -d < ~/.config/smagent/sse-c/sm-raw.key.b64)
```

A key that fails its fingerprint is refused before any request, so a wrong key cannot start
encrypting part of the mirror under a second key.

## Rotating the key

An owner action, done deliberately and in one sitting. S3 re-encrypts an SSE-C object only by
copying it onto itself with both keys:

```bash
aws s3api copy-object --bucket "$L0_S3_BUCKET" --key sm-raw/<path> \
  --copy-source "$L0_S3_BUCKET/sm-raw/<path>" \
  --copy-source-sse-customer-algorithm AES256 \
  --copy-source-sse-customer-key fileb://<(base64 -d < old.key.b64) \
  --sse-customer-algorithm AES256 \
  --sse-customer-key fileb://<(base64 -d < new.key.b64)
```

Loop that over the listing (one `copy-object` handles objects up to 5 GB). It is the one sanctioned
rewrite of an existing key: the bytes are unchanged, only their encryption. Versioning keeps the
old version under the **old** key, so keep the old key for as long as noncurrent versions exist.
Once every current object is re-encrypted, install the new key and fingerprint in place of the
old, and run `probe` and `verify` — a half-done rotation shows up as download failures under the
new key.

## Tooling

The AWS CLI is a uv tool (`uv tool install awscli`, at `~/.local/bin/aws`) — never pip into the
host. Credentials are the instance role. The script is checked offline by
`tests/unit/test_l0_s3_sync.py` against a stub `aws`, and by `shellcheck`
(`uv tool run --from shellcheck-py shellcheck ops/l0-s3-sync.sh`).
