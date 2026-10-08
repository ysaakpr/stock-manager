#!/usr/bin/env bash
# Mirror the L0 lake to S3 under an SSE-C customer key (OPS), and verify the mirror.
#
#   ops/l0-s3-sync.sh sync              upload every L0 file S3 does not have yet (the default)
#   ops/l0-s3-sync.sh verify [SAMPLE]   every L0 file present with equal size, nothing extra, and
#                                       SAMPLE (default 300) random objects re-downloaded and
#                                       compared by sha256 — under SSE-C the ETag is not an MD5,
#                                       so a listing alone cannot prove content
#   ops/l0-s3-sync.sh probe             decode the key, check it, and head one object with it
#
# Local data/L0 stays the record the pipeline reads and writes; this is a copy of it. L0 is
# immutable (invariant #1), so the mirror only ever grows: `sync` never deletes on the S3 side.
#
# **The key.** Stored base64-encoded (a text file survives copy-paste into a password manager; 32
# raw bytes do not). It is decoded only into a pipe handed to the AWS CLI as `fileb:///dev/fd/N`:
# never onto disk, never into argv, never into a log. Its sha256 prefix is checked against the
# fingerprint file when one exists, so a mangled or swapped key fails here rather than as a wall
# of 403s — or worse, as a second key silently encrypting half the mirror.
#
# **Where.** Bucket and prefix come from the untracked ~/.config/smagent/l0-s3.env (or the
# environment). They are not in this file because the repo is public and the bucket name carries
# the account id.
#
#   L0_S3_BUCKET        required
#   L0_S3_PREFIX        default sm-raw
#   SSE_C_KEY_B64       default ~/.config/smagent/sse-c/sm-raw.key.b64
#   SSE_C_FINGERPRINT   default <key file without .b64>.fingerprint (skipped if absent)
#   DATA_ROOT           default <this repo>/data; L0 is $DATA_ROOT/L0
#   L0_S3_ENV           default ~/.config/smagent/l0-s3.env
#
# Exit codes: 0 ok · 1 verification failed · 2 configuration missing or invalid · 3 another sync
# holds the lock · 4 no usable `aws`.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${L0_S3_ENV:-$HOME/.config/smagent/l0-s3.env}"
if [[ -r "$env_file" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$env_file"; set +a
fi

: "${L0_S3_PREFIX:=sm-raw}"
: "${SSE_C_KEY_B64:=$HOME/.config/smagent/sse-c/sm-raw.key.b64}"
: "${SSE_C_FINGERPRINT:=${SSE_C_KEY_B64%.b64}.fingerprint}"
: "${DATA_ROOT:=$here/data}"
l0="$DATA_ROOT/L0"
prefix="${L0_S3_PREFIX%/}/"

die() { echo "l0-s3-sync: $1" >&2; exit "$2"; }

[[ -n "${L0_S3_BUCKET:-}" ]] || die "L0_S3_BUCKET is not set (expected in $env_file)" 2
[[ -d "$l0" ]] || die "no L0 at $l0" 2
[[ -r "$SSE_C_KEY_B64" ]] || die "SSE-C key not readable at $SSE_C_KEY_B64" 2

# `aws` is a uv tool in ~/.local/bin, which a systemd user unit's PATH does not include.
aws_bin="${AWS_BIN:-$(command -v aws || true)}"
[[ -z "$aws_bin" && -x "$HOME/.local/bin/aws" ]] && aws_bin="$HOME/.local/bin/aws"
[[ -x "$aws_bin" ]] || die "no executable aws found (tried \$AWS_BIN, PATH, ~/.local/bin/aws)" 4

# Decoded bytes go to stdout only; every caller reads them through process substitution.
key_bytes() { base64 -d < "$SSE_C_KEY_B64"; }

check_key() {
    local n
    n="$(key_bytes | wc -c)" || die "SSE-C key at $SSE_C_KEY_B64 is not valid base64" 2
    [[ "$n" -eq 32 ]] || die "SSE-C key decodes to $n bytes, AES256 needs 32" 2
    if [[ -r "$SSE_C_FINGERPRINT" ]]; then
        [[ "$(key_bytes | sha256sum | cut -c1-16)" == "$(tr -d '[:space:]' < "$SSE_C_FINGERPRINT")" ]] \
            || die "SSE-C key does not match its fingerprint $SSE_C_FINGERPRINT" 2
    fi
}

# One sync at a time: two would double the upload and race the verify listing.
exec 9> "${XDG_RUNTIME_DIR:-/tmp}/l0-s3-sync.lock"
flock -n 9 || die "another l0-s3-sync is running" 3

check_key
mode="${1:-sync}"
echo "l0-s3-sync: mode=$mode l0=$l0 dest=s3://$L0_S3_BUCKET/$prefix at $(TZ=Asia/Kolkata date '+%F %T IST')"

case "$mode" in
    sync)
        "$aws_bin" s3 sync "$l0/" "s3://$L0_S3_BUCKET/$prefix" --only-show-errors \
            --sse-c AES256 --sse-c-key "fileb://"<(key_bytes)
        echo "l0-s3-sync: sync done at $(TZ=Asia/Kolkata date '+%F %T IST')"
        ;;
    verify)
        sample="${2:-300}"
        work="$(mktemp -d)"
        trap 'rm -rf "$work"' EXIT
        (cd "$l0" && find . -type f -printf '%P\t%s\n' | LC_ALL=C sort) > "$work/local.tsv"
        "$aws_bin" s3api list-objects-v2 --bucket "$L0_S3_BUCKET" --prefix "$prefix" \
            --query 'Contents[].[Key,Size]' --output text \
            | sed "s#^$prefix##" | LC_ALL=C sort > "$work/remote.tsv"
        missing="$(LC_ALL=C comm -23 "$work/local.tsv" "$work/remote.tsv" | wc -l)"
        extra="$(LC_ALL=C comm -13 "$work/local.tsv" "$work/remote.tsv" | wc -l)"
        echo "local files: $(wc -l < "$work/local.tsv")  remote objects: $(wc -l < "$work/remote.tsv")"
        echo "missing or size-mismatched: $missing  extra or size-mismatched: $extra"
        LC_ALL=C comm -23 "$work/local.tsv" "$work/remote.tsv" | head -10
        mismatched=0
        while read -r rel; do
            "$aws_bin" s3 cp "s3://$L0_S3_BUCKET/$prefix$rel" "$work/obj" --only-show-errors \
                --sse-c AES256 --sse-c-key "fileb://"<(key_bytes)
            if [[ "$(sha256sum < "$work/obj")" != "$(sha256sum < "$l0/$rel")" ]]; then
                echo "sha256 mismatch: $rel"
                mismatched=$((mismatched + 1))
            fi
        done < <(shuf -n "$sample" "$work/local.tsv" | cut -f1)
        echo "sampled sha256 mismatches: $mismatched of $sample"
        if [[ "$missing" -eq 0 && "$extra" -eq 0 && "$mismatched" -eq 0 ]]; then
            echo "l0-s3-sync: VERIFIED"
        else
            echo "l0-s3-sync: NOT VERIFIED"; exit 1
        fi
        ;;
    probe)
        first="$(cd "$l0" && find . -type f -printf '%P\n' -quit)"
        "$aws_bin" s3api head-object --bucket "$L0_S3_BUCKET" --key "$prefix$first" \
            --sse-customer-algorithm AES256 --sse-customer-key "fileb://"<(key_bytes) \
            --query SSECustomerAlgorithm --output text
        echo "l0-s3-sync: key ok against $prefix$first"
        ;;
    *)
        die "unknown mode '$mode' (sync | verify [SAMPLE] | probe)" 2
        ;;
esac
