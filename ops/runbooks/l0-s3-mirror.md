# L0 S3 mirror (SSE-C)

`data/L0` is copied to S3 with an SSE-C customer key, by `ops/l0-s3-sync.sh`. It is a **copy**:
the pipeline still reads and writes local `data/L0`, which stays the authoritative record until
the storage layer learns to write to S3 itself (the AWS move). L0 is immutable, so the mirror only
grows — `sync` never deletes anything in S3.

## What lives where (none of it in this repo)

| What | Where | Mode |
|---|---|---|
| Bucket and prefix (`L0_S3_BUCKET`, `L0_S3_PREFIX`) | `~/.config/smagent/l0-s3.env` | 600 |
| SSE-C key, 32 bytes, base64 | `~/.config/smagent/sse-c/sm-raw.key.b64` | 400 |
| Key fingerprint (sha256 of the raw bytes, first 16 hex) | `~/.config/smagent/sse-c/sm-raw.key.fingerprint` | 644 |

The bucket name carries the AWS account id and the repo is public, so it stays in the untracked
env file. **The key is the only way to read the mirror.** Lose it and every object is unreadable;
keep a second copy off this machine (the base64 text is one line, made for a password manager).

## Use

```bash
ops/l0-s3-sync.sh probe          # key decodes, is 32 bytes, matches its fingerprint, opens an object
ops/l0-s3-sync.sh sync           # upload what S3 is missing
ops/l0-s3-sync.sh verify 300     # keys + sizes match exactly; 300 random objects match by sha256
```

From a worktree, set `DATA_ROOT=/home/ubuntu/stock-manager/data` — the worktree has no lake.
A full listing compare over ~224k objects takes several minutes even when nothing is new.

Exit codes: 0 ok · 1 verify failed · 2 config/key invalid · 3 another sync running · 4 no `aws`.

`verify` downloads a sample because under SSE-C the ETag is not the content MD5: a listing proves
names and sizes, not bytes. A `missing` count right after a sync is normally files an L0 writer
(the scheduler, a campaign) created mid-sync — run `sync` again and re-verify.

## How the key is handled

Decoded only into a pipe and given to the AWS CLI as `fileb:///dev/fd/N`: never written to disk
decoded, never on a command line, never logged. Do the same in any ad-hoc command:

```bash
aws s3 cp s3://$L0_S3_BUCKET/sm-raw/<path> out --sse-c AES256 \
  --sse-c-key fileb://<(base64 -d < ~/.config/smagent/sse-c/sm-raw.key.b64)
```

A key that fails its fingerprint is refused before any request, so a wrong key cannot start
encrypting part of the mirror under a second key.

The AWS CLI is a uv tool (`uv tool install awscli`, at `~/.local/bin/aws`) — never pip into the
host. Credentials are the instance role.
