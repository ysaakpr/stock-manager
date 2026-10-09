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

__all__ = ["MASK", "mask_secrets"]

#: What a masked value becomes.
MASK: Final[str] = "***"

# A PEM private-key block, header to footer, whatever is between.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----.*?-----END (?P=kind)-----", re.DOTALL
)
# Prefix-identified tokens: Anthropic/OpenAI-style `sk-…`, GitHub `ghp_…` and friends, Slack
# `xox?-…`, AWS access key ids. Each prefix is distinctive enough that prose does not match.
_PREFIXED_TOKEN = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16})"
)
# `Bearer <token>` / `Basic <b64>`: the credential is the word *after* the scheme, which the
# name=value rule below would miss (it would mask the scheme and keep the token).
_AUTH_SCHEME = re.compile(r"\b(?P<scheme>Bearer|Basic)(?P<sep>\s+)[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_URL_QUERY = re.compile(r"(?P<path>://[^\s?#]+)\?[^\s#]*")
_TELEGRAM_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_CREDENTIAL_PAIR = re.compile(
    r"(?P<name>password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization)"
    r"(?P<sep>\s*[=:]\s*)(?P<value>\S+)",
    re.IGNORECASE,
)


def mask_secrets(text: str, *, drop_url_queries: bool = True) -> str:
    """`text` with anything credential-shaped replaced by `MASK`, layout otherwise untouched.

    What it does: masks PEM private keys, prefix-identified API tokens (`sk-…`, `ghp_…`, `AKIA…`),
    `Bearer`/`Basic` credentials, URL userinfo (a DSN's `user:password@`), URL query strings (where
    a token travels when it travels in a URL) unless `drop_url_queries` is false, Telegram bot
    tokens, and the value of any `password=`/`token:`-style pair.
    What it assumes: the text is prose or diagnostics, not data whose every byte matters.
    What it never does: collapse whitespace or truncate — that is the caller's choice — or pass a
    secret through because it was "already public" on the page it came from.
    """
    text = _PRIVATE_KEY.sub(f"-----BEGIN \\g<kind>-----{MASK}-----END \\g<kind>-----", text)
    text = _PREFIXED_TOKEN.sub(MASK, text)
    text = _AUTH_SCHEME.sub(rf"\g<scheme>\g<sep>{MASK}", text)
    text = _URL_USERINFO.sub(rf"\g<scheme>{MASK}@", text)
    if drop_url_queries:
        text = _URL_QUERY.sub(rf"\g<path>?{MASK}", text)
    text = _TELEGRAM_TOKEN.sub(f"bot{MASK}", text)
    return _CREDENTIAL_PAIR.sub(rf"\g<name>\g<sep>{MASK}", text)
