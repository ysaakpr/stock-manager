"""Repo-local detect-secrets detectors for credentials the stock plugins miss (M15.1).

Loaded by ops/secret_scan.py through `.secrets.baseline`'s `plugins_used` (a `file://` entry). The
stock KeywordDetector has no "token" in its keyword list, so `KITE_ACCESS_TOKEN=<value>` — the
credential that can move real money — would pass it; nor does any stock plugin know Anthropic's
key prefix. Each pattern has exactly one capturing group, the value: detect-secrets hashes and
reports that group, and the baseline's false-positive entries are keyed on its hash.
"""

from __future__ import annotations

import re

from detect_secrets.plugins.base import RegexBasedDetector

# A credential-shaped value: 16+ characters of a token alphabet with at least one letter and one
# digit, so prose (`access_token: the daily session credential`) and placeholders without digits
# (`your_access_token_here`) are not findings.
_VALUE = r"(?=[A-Za-z0-9_\-.+/=]*[0-9])(?=[A-Za-z0-9_\-.+/=]*[A-Za-z])([A-Za-z0-9_\-.+/=]{16,})"


class TokenAssignmentDetector(RegexBasedDetector):
    """`<something>_token = <value>` in any syntax: Python, YAML, JSON, .env, shell."""

    secret_type = "Token Assignment"

    denylist = (
        re.compile(
            r"(?:access|refresh|request|session|bearer|auth|api|bot|enc)[_\-]?token"
            r"[\"']?\s*(?::=|=|:)\s*[\"']?" + _VALUE,
            flags=re.IGNORECASE,
        ),
    )


class AnthropicKeyDetector(RegexBasedDetector):
    """An Anthropic API or admin key, wherever it appears — no assignment needed."""

    secret_type = "Anthropic API Key"

    denylist = (re.compile(r"\b(sk-ant-[A-Za-z0-9]{2,8}-[A-Za-z0-9_\-]{20,})"),)
