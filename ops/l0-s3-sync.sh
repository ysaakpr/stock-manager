#!/usr/bin/env bash
# Mirror the L0 lake to S3 under an SSE-C customer key (OPS), and verify the mirror.
#
#   ops/l0-s3-sync.sh sync              upload every settled L0 file whose key S3 does not have
#                                       yet (the default); an existing key is never re-uploaded
#   ops/l0-s3-sync.sh verify [SAMPLE]   every settled L0 file present with equal size, nothing
#                                       extra, and SAMPLE (default 300) random objects re-downloaded
#                                       and compared by sha256 with the local file and, when it is
#                                       there, the l0_backup manifest — under SSE-C the ETag is not
#                                       an MD5, so a listing alone cannot prove content
#   ops/l0-s3-sync.sh probe             check the key, then head one object from the S3 listing with
#                                       it and compare S3's SSECustomerKeyMD5 with the key's own
#
# Local data/L0 stays the record the pipeline reads and writes; this is a copy of it. L0 is
# immutable (invariant #1), so the mirror only ever grows: `sync` lists the keys already in S3 and
# uploads only names that are not there. It never deletes, and it never overwrites — not even an
# object whose size or mtime differs, because a differing L0 file is an incident for the owner, not
# drift for a sync to paper over. `probe` and `sync` refuse a bucket without versioning or Object
# Lock, so a mistaken overwrite from anywhere else still leaves the original bytes recoverable.
#
# **Settling.** A file modified in the last L0_S3_SETTLE_MINUTES (default 10, the same margin as
# l0_backup's `_SETTLE`) is deferred to the next run: once uploaded a key is frozen, so a payload
# caught half-written would stay half-written in the mirror for good.
#
# **The key.** Stored base64-encoded (a text file survives copy-paste into a password manager; 32
# raw bytes do not). It is decoded only into a pipe handed to the AWS CLI as `fileb:///dev/fd/N`:
# never onto disk, never into argv, never into a log. Its sha256 prefix must match the fingerprint
# file, so a mangled or swapped key fails here rather than as a wall of 403s — or worse, as a
# second key silently encrypting half the mirror.
#
# **Where.** Bucket and prefix come from the untracked ~/.config/smagent/l0-s3.env (or the
# environment). They are not in this file because the repo is public and the bucket name carries
# the account id — which is also why nothing here prints the bucket, only the prefix.
#
#   L0_S3_BUCKET          required
#   L0_S3_PREFIX          default sm-raw
#   SSE_C_KEY_B64         default ~/.config/smagent/sse-c/sm-raw.key.b64 (yours, mode 600 or 400)
#   SSE_C_FINGERPRINT     default <key file without .b64>.fingerprint (required)
#   DATA_ROOT             default <this repo>/data; L0 is $DATA_ROOT/L0
#   L0_S3_ENV             default ~/.config/smagent/l0-s3.env (yours, no group/other bits)
#   L0_S3_SETTLE_MINUTES  default 10
#   L0_MANIFEST           default ~/backups/l0/MANIFEST.sha256 (verify uses it when present)
#
# Exit codes: 0 ok · 1 sync or verification failed · 2 configuration missing or invalid · 3 another
# sync holds the lock · 4 no usable `aws`.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
me="${USER:-$(id -un)}"

die() { echo "l0-s3-sync: $1" >&2; exit "$2"; }

# Owned by the caller and nothing for group/other: the env file is sourced (a writable one is code
# execution as us) and names the bucket; the key file is the only way to read the mirror.
check_private() {
    local file="$1" what="$2" perm owner
    read -r perm owner < <(stat -c '%a %U' "$file") || die "cannot stat $what $file" 2
    [[ "$owner" == "$me" ]] || die "$what $file is owned by $owner, not $me" 2
    (( (8#$perm & 8#077) == 0 )) \
        || die "$what $file is mode $perm; it must have no group/other bits (chmod 600)" 2
}

env_file="${L0_S3_ENV:-$HOME/.config/smagent/l0-s3.env}"
if [[ -e "$env_file" ]]; then
    check_private "$env_file" "env file"
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
fi

: "${L0_S3_PREFIX:=sm-raw}"
: "${SSE_C_KEY_B64:=$HOME/.config/smagent/sse-c/sm-raw.key.b64}"
: "${SSE_C_FINGERPRINT:=${SSE_C_KEY_B64%.b64}.fingerprint}"
: "${DATA_ROOT:=$here/data}"
: "${L0_S3_SETTLE_MINUTES:=10}"
: "${L0_MANIFEST:=$HOME/backups/l0/MANIFEST.sha256}"
l0="$DATA_ROOT/L0"
prefix="${L0_S3_PREFIX%/}/"

[[ -n "${L0_S3_BUCKET:-}" ]] || die "L0_S3_BUCKET is not set (expected in $env_file)" 2
[[ -d "$l0" ]] || die "no L0 at $l0" 2
[[ "$L0_S3_SETTLE_MINUTES" =~ ^[0-9]+$ ]] || die "L0_S3_SETTLE_MINUTES must be whole minutes" 2
[[ -e "$SSE_C_KEY_B64" ]] || die "SSE-C key not found at $SSE_C_KEY_B64" 2
check_private "$SSE_C_KEY_B64" "SSE-C key"
case "$(stat -c '%a' "$SSE_C_KEY_B64")" in
    600|400) ;;
    *) die "SSE-C key $SSE_C_KEY_B64 must be mode 600 or 400" 2 ;;
esac
[[ -r "$SSE_C_KEY_B64" ]] || die "SSE-C key not readable at $SSE_C_KEY_B64" 2
[[ -r "$SSE_C_FINGERPRINT" ]] || die "SSE-C key fingerprint not readable at $SSE_C_FINGERPRINT" 2

# `aws` is a uv tool in ~/.local/bin, which a systemd user unit's PATH does not include.
aws_bin="${AWS_BIN:-$(command -v aws || true)}"
[[ -z "$aws_bin" && -x "$HOME/.local/bin/aws" ]] && aws_bin="$HOME/.local/bin/aws"
[[ -x "$aws_bin" ]] || die "no executable aws found (tried \$AWS_BIN, PATH, ~/.local/bin/aws)" 4
bucket="$L0_S3_BUCKET"

# Decoded bytes go to stdout only; every caller reads them through process substitution.
key_bytes() { base64 -d < "$SSE_C_KEY_B64"; }

check_key() {
    local n
    n="$(key_bytes | wc -c)" || die "SSE-C key at $SSE_C_KEY_B64 is not valid base64" 2
    [[ "$n" -eq 32 ]] || die "SSE-C key decodes to $n bytes, AES256 needs 32" 2
    [[ "$(key_bytes | sha256sum | cut -c1-16)" == "$(tr -d '[:space:]' < "$SSE_C_FINGERPRINT")" ]] \
        || die "SSE-C key does not match its fingerprint $SSE_C_FINGERPRINT" 2
}

# Versioning (or Object Lock, which implies it) keeps every overwritten or deleted version, so the
# never-overwrite rule does not rest on this script alone.
require_versioning() {
    local status lock
    status="$("$aws_bin" s3api get-bucket-versioning --bucket "$bucket" \
        --query Status --output text)" \
        || die "could not read bucket versioning (is s3:GetBucketVersioning allowed?)" 2
    [[ "$status" == "Enabled" ]] && return 0
    lock="$("$aws_bin" s3api get-object-lock-configuration --bucket "$bucket" \
        --query ObjectLockConfiguration.ObjectLockEnabled --output text 2> /dev/null)" || lock=""
    [[ "$lock" == "Enabled" ]] && return 0
    die "bucket versioning is '$status' and Object Lock is not configured: refusing to write; enable versioning first (ops/runbooks/l0-s3-mirror.md)" 2
}

# Objects under the prefix with the prefix stripped, one per line. `--output text` prints `None`
# for an empty listing (or page), which is not an object.
remote_listing() {
    "$aws_bin" s3api list-objects-v2 --bucket "$bucket" --prefix "$prefix" \
        --query "$1" --output text \
        | awk -v p="$prefix" '$0 != "None" && $0 != "" {
              if (index($0, p) == 1) $0 = substr($0, length(p) + 1)
              print
          }'
}

# Files under L0 unmodified for the settle window, in find's -printf format $1.
local_settled() {
    (cd "$l0" && find . -type f -mmin "+$L0_S3_SETTLE_MINUTES" -printf "$1")
}

now_ist() { TZ=Asia/Kolkata date '+%F %T IST'; }

# One sync at a time: two would double the upload and race the verify listing. A fixed path under
# $HOME, so a login shell, a systemd unit and a cron line all contend for the same lock.
lock_dir="$HOME/.local/state/smagent"
mkdir -p "$lock_dir"
exec 9> "$lock_dir/l0-s3-sync.lock"
flock -n 9 || die "another l0-s3-sync is running" 3

check_key
mode="${1:-sync}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "l0-s3-sync: mode=$mode l0=$l0 prefix=$prefix at $(now_ist)"

case "$mode" in
    sync)
        require_versioning
        local_settled '%P\n' | LC_ALL=C sort > "$work/local"
        total="$(cd "$l0" && find . -type f | wc -l)"
        deferred=$((total - $(wc -l < "$work/local")))
        remote_listing 'Contents[].[Key]' | LC_ALL=C sort > "$work/remote"
        # Names only: a key already in S3 is never uploaded again, whatever its size or mtime.
        LC_ALL=C comm -23 "$work/local" "$work/remote" > "$work/todo"
        todo="$(wc -l < "$work/todo")"
        echo "local settled: $(wc -l < "$work/local")  deferred (modified <" \
            "$L0_S3_SETTLE_MINUTES min ago): $deferred  remote: $(wc -l < "$work/remote")" \
            " to upload: $todo"
        uploaded=0
        failed=0
        while IFS= read -r rel; do
            if "$aws_bin" s3 cp "$l0/$rel" "s3://$bucket/$prefix$rel" --only-show-errors \
                --sse-c AES256 --sse-c-key "fileb://"<(key_bytes); then
                uploaded=$((uploaded + 1))
            else
                echo "upload failed: $rel" >&2
                failed=$((failed + 1))
            fi
            if (((uploaded + failed) % 1000 == 0)); then
                echo "progress: $((uploaded + failed)) of $todo"
            fi
        done < "$work/todo"
        echo "l0-s3-sync: sync done at $(now_ist): uploaded $uploaded, failed $failed," \
            "deferred $deferred"
        [[ "$failed" -eq 0 ]] || exit 1
        ;;
    verify)
        sample="${2:-300}"
        [[ "$sample" =~ ^[0-9]+$ ]] || die "SAMPLE must be a whole number" 2
        local_settled '%P\t%s\n' | LC_ALL=C sort > "$work/local.tsv"
        remote_listing 'Contents[].[Key,Size]' | LC_ALL=C sort > "$work/remote.tsv"
        missing="$(LC_ALL=C comm -23 "$work/local.tsv" "$work/remote.tsv" | wc -l)"
        extra="$(LC_ALL=C comm -13 "$work/local.tsv" "$work/remote.tsv" | wc -l)"
        echo "local settled files: $(wc -l < "$work/local.tsv")" \
            " remote objects: $(wc -l < "$work/remote.tsv")"
        echo "missing or size-mismatched: $missing  extra or size-mismatched: $extra"
        LC_ALL=C comm -23 "$work/local.tsv" "$work/remote.tsv" | head -10

        shuf -n "$sample" "$work/local.tsv" | cut -f1 > "$work/sample"
        sampled="$(wc -l < "$work/sample")"
        # The manifest names files relative to DATA_ROOT (`<sha256>  L0/<path>`); keep the sample's.
        : > "$work/manifest"
        if [[ -r "$L0_MANIFEST" ]]; then
            awk 'NR == FNR { want["L0/" $0] = 1; next }
                 { name = substr($0, 67) }
                 name in want { print name "\t" $1 }' \
                "$work/sample" "$L0_MANIFEST" > "$work/manifest"
            manifest_note="$(wc -l < "$work/manifest") of them in $L0_MANIFEST"
        else
            manifest_note="no manifest at $L0_MANIFEST"
        fi
        mismatched=0
        unreadable=0
        manifest_bad=0
        while IFS= read -r rel; do
            rm -f "$work/obj"
            if ! "$aws_bin" s3 cp "s3://$bucket/$prefix$rel" "$work/obj" --only-show-errors \
                --sse-c AES256 --sse-c-key "fileb://"<(key_bytes); then
                echo "download failed: $rel"
                unreadable=$((unreadable + 1))
                continue
            fi
            got="$(sha256sum < "$work/obj" | cut -d' ' -f1)"
            if [[ "$got" != "$(sha256sum < "$l0/$rel" | cut -d' ' -f1)" ]]; then
                echo "sha256 mismatch with local: $rel"
                mismatched=$((mismatched + 1))
            fi
            recorded="$(awk -F'\t' -v n="L0/$rel" '$1 == n { print $2; exit }' "$work/manifest")"
            if [[ -n "$recorded" && "$recorded" != "$got" ]]; then
                echo "sha256 mismatch with manifest: $rel"
                manifest_bad=$((manifest_bad + 1))
            fi
        done < "$work/sample"
        echo "sampled: $sampled ($manifest_note)  download failures: $unreadable" \
            " sha256 mismatches: local $mismatched, manifest $manifest_bad"
        if ((missing + extra + unreadable + mismatched + manifest_bad == 0)); then
            echo "l0-s3-sync: VERIFIED"
        else
            echo "l0-s3-sync: NOT VERIFIED"
            exit 1
        fi
        ;;
    probe)
        require_versioning
        first="$("$aws_bin" s3api list-objects-v2 --bucket "$bucket" --prefix "$prefix" \
            --max-items 1 --query 'Contents[].[Key]' --output text \
            | awk '$0 != "None" && $0 != "" { print; exit }')"
        [[ -n "$first" ]] || die "nothing under $prefix to probe; the key was checked locally only" 1
        command -v openssl > /dev/null || die "probe needs openssl for the key's MD5" 4
        md5="$("$aws_bin" s3api head-object --bucket "$bucket" --key "$first" \
            --sse-customer-algorithm AES256 --sse-customer-key "fileb://"<(key_bytes) \
            --query SSECustomerKeyMD5 --output text)" \
            || die "head-object with this key failed on $first" 1
        [[ "$md5" == "$(key_bytes | openssl dgst -md5 -binary | base64)" ]] \
            || die "S3 reports a different SSE-C key MD5 for $first" 1
        echo "l0-s3-sync: key ok against $first"
        ;;
    *)
        die "unknown mode '$mode' (sync | verify [SAMPLE] | probe)" 2
        ;;
esac
