"""Credential masking for text that leaves its source: alerts, stored web pages (invariant #13).

One set of patterns, so an alert body and a stored Commons web snapshot are masked by the same
rules. Two callers today: `dataplatform.alert_triggers.redact` (which then collapses whitespace and
truncates to an alert-sized detail) and `analyst.commons.fetch` (which keeps the page's layout).

The patterns are over-eager by design. An over-eager mask costs a little detail; an under-eager one
publishes a credential, and in an append-only store or a pushed alert that cannot be taken back.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["MASK", "mask_and_truncate", "mask_secrets"]

#: What a masked value becomes.
MASK: Final[str] = "***"

# A PEM private-key block, header to footer, whatever is between.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----.*?-----END (?P=kind)-----", re.DOTALL
)
# Prefix-identified tokens: Anthropic/OpenAI-style `sk-…`, GitHub `ghp_…` and friends, Slack
# `xox?-…`, AWS access key ids, Google `AIza…` keys, Stripe secret and restricted keys, GitLab
# personal access tokens, Hugging Face `hf_…`, npm `npm_…`. Each prefix is distinctive enough that
# prose does not match.
_PREFIXED_TOKEN = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|[sr]k_(?:live|test)_[0-9A-Za-z]{16,}"
    r"|glpat-[0-9A-Za-z_-]{20,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|npm_[A-Za-z0-9]{30,})"
)
# A JWT: three base64url segments, the first a JSON header (`{"` encodes to `eyJ`). The signature
# may be empty (`alg: none`), which is still a bearer credential to whatever accepts it.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
# `Bearer <token>` / `Basic <b64>`: the credential is the word *after* the scheme, which the
# name=value rule below would miss (it would mask the scheme and keep the token).
_AUTH_SCHEME = re.compile(r"\b(?P<scheme>Bearer|Basic)(?P<sep>\s+)[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_URL_QUERY = re.compile(r"(?P<path>://[^\s?#]+)\?[^\s#]*")
# Webhook URLs whose path *is* the credential: anyone holding a Slack or Discord webhook URL can
# post as the integration. Everything after the fixed prefix goes.
_WEBHOOK_PATH = re.compile(
    r"(?P<prefix>hooks\.slack\.com/(?:services|workflows|triggers)/"
    r"|discord(?:app)?\.com/api/webhooks/)[^\s?#\"'<>]+"
)
# Any other URL path: a whole segment that reads like a random token — 28+ letters and digits, no
# separators, mixing upper case, lower case and digits — is masked. A slug has separators, a numeric
# id has no letters and a hex digest has no upper case, so none of them is. The floor sits above a
# YouTube channel id (24), which a scraped page carries legitimately and which is not a credential.
_URL_PATH = re.compile(r"(?P<origin>[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s?#]+)(?P<path>/[^\s?#\"'<>]*)")
# A segment followed by a document extension is a filename (`AnnualReport2026FinalVersionQ2.pdf`),
# not a token, and is kept: exchange filings are routinely named that way.
_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)"
    r"[A-Za-z0-9]{28,}(?![A-Za-z0-9_-])"
    r"(?!\.(?i:pdf|xlsx?|xlsm|csv|docx?|pptx?|html?|xml|json|txt|zip)(?![A-Za-z0-9]))"
)
_TELEGRAM_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_CREDENTIAL_NAME = r"password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization"
# A quoted pair — JSON's `"api_key": "…"` or a Python repr's `'token': '…'`. The bare rule below
# cannot see these: the closing quote sits between the name and the colon. The credential word must
# end the key and start it or follow a `_`/`-`/`.` or a camelCase hump, so `"access_token"`,
# `"clientSecret"` and `"apiKey"` are masked while `"company_secretary"`, `"input_tokens"`,
# `"authorization_date"` and `"tokenised_shares"` — fields that are not credentials — are not.
_QUOTED_PAIR = re.compile(
    r"(?P<q>[\"'])(?P<name>(?:[^\"'\s]*(?:[_.-]|(?-i:(?<=[a-z])(?=[A-Z]))))?"
    rf"(?:{_CREDENTIAL_NAME}))(?P=q)(?P<sep>\s*:\s*)"
    r"(?P<vq>[\"'])(?:\\.|(?!(?P=vq)).)*(?P=vq)",
    re.IGNORECASE,
)
_CREDENTIAL_PAIR = re.compile(
    rf"(?P<name>{_CREDENTIAL_NAME})(?P<sep>\s*[=:]\s*)(?P<value>\S+)",
    re.IGNORECASE,
)


def _mask_path_tokens(match: re.Match[str]) -> str:
    return match["origin"] + _PATH_TOKEN.sub(MASK, match["path"])


def mask_secrets(text: str, *, drop_url_queries: bool = True) -> str:
    """`text` with anything credential-shaped replaced by `MASK`, layout otherwise untouched.

    What it does: masks PEM private keys, prefix-identified API tokens (`sk-…`, `ghp_…`, `AKIA…`,
    `AIza…`, `sk_live_…`, `glpat-…`, `hf_…`, `npm_…`), JWTs, `Bearer`/`Basic` credentials, URL
    userinfo (a DSN's `user:password@`), webhook URL paths and token-shaped URL path segments, URL
    query strings (where a token travels when it travels in a URL) unless `drop_url_queries` is
    false, Telegram bot tokens, and the value of any `password=`/`token:`-style pair, bare or
    quoted (`"api_key": "…"`).
    What it assumes: the text is prose or diagnostics, not data whose every byte matters.
    What it never does: collapse whitespace or truncate — that is the caller's choice — or pass a
    secret through because it was "already public" on the page it came from.
    """
    text = _PRIVATE_KEY.sub(f"-----BEGIN \\g<kind>-----{MASK}-----END \\g<kind>-----", text)
    text = _PREFIXED_TOKEN.sub(MASK, text)
    text = _JWT.sub(MASK, text)
    text = _AUTH_SCHEME.sub(rf"\g<scheme>\g<sep>{MASK}", text)
    text = _URL_USERINFO.sub(rf"\g<scheme>{MASK}@", text)
    text = _WEBHOOK_PATH.sub(rf"\g<prefix>{MASK}", text)
    text = _URL_PATH.sub(_mask_path_tokens, text)
    if drop_url_queries:
        text = _URL_QUERY.sub(rf"\g<path>?{MASK}", text)
    text = _TELEGRAM_TOKEN.sub(f"bot{MASK}", text)
    text = _QUOTED_PAIR.sub(rf"\g<q>\g<name>\g<q>\g<sep>\g<vq>{MASK}\g<vq>", text)
    return _CREDENTIAL_PAIR.sub(rf"\g<name>\g<sep>{MASK}", text)


def mask_and_truncate(text: str, limit: int) -> str:
    """`mask_secrets(text)`, then cut to at most `limit` characters (an ellipsis marks a cut).

    What it does: masks first, so a cut can never leave half a credential the patterns no longer
    recognise, then bounds the result for a store or a message that must stay small.
    What it assumes: `limit` is at least 1.
    What it never does: collapse whitespace (`alert_triggers.redact` does, for alert bodies).
    """
    masked = mask_secrets(text)
    return masked if len(masked) <= limit else masked[: limit - 1] + "…"
