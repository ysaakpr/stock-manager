"""Repo-local detect-secrets detectors for credentials the stock plugins miss (M15.1).

Loaded by ops/secret_scan.py through `.secrets.baseline`'s `plugins_used` (`file://` entries). The
stock KeywordDetector has no "token" in its keyword list, so `KITE_ACCESS_TOKEN=<value>` — the
credential that can move real money — passes it; no stock plugin knows Anthropic's key prefix, an
`Authorization:` header, a bare Kite-shaped token, or a password inside a libpq conninfo string.

Every pattern has exactly one capturing group, the value: detect-secrets hashes and reports that
group, and the baseline's false-positive entries are keyed on its hash.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import PurePath
from typing import Any, cast

from detect_secrets.core.potential_secret import PotentialSecret
from detect_secrets.plugins.base import RegexBasedDetector
from detect_secrets.util.code_snippet import CodeSnippet

# A credential-shaped value: 16+ characters of a token alphabet with at least one letter and one
# digit, so prose (`access_token: the daily session credential`) and digit-free placeholders
# (`your_access_token_here`) are not findings.
_CHARS = r"A-Za-z0-9_\-.+/="
_VALUE = rf"(?=[{_CHARS}]*[0-9])(?=[{_CHARS}]*[A-Za-z])([{_CHARS}]{{16,}})"


class TokenAssignmentDetector(RegexBasedDetector):
    """A token assigned or passed: `KITE_ACCESS_TOKEN=…`, `token: …`, `kite.set_access_token("…")`.

    Any name ending in `token` (KITE_TOKEN, kite_token, access_token, bare token), in Python, YAML,
    JSON, .env or shell syntax, plus the call form `…token("<value>")`. A value split across lines
    inside parentheses is not seen — line-based scanning; documented in the runbook.
    """

    secret_type = "Token Assignment"

    denylist = (
        re.compile(r"\w*token[\"']?\s*(?::=|=|:)\s*[\"']?" + _VALUE, flags=re.IGNORECASE),
        re.compile(r"\w*token\(\s*[\"']" + _VALUE, flags=re.IGNORECASE),
    )


class AuthorizationHeaderDetector(RegexBasedDetector):
    """`Authorization: token <api_key>:<access_token>` (Kite) and `Authorization: Bearer <tok>`."""

    secret_type = "Authorization Header"

    denylist = (
        re.compile(
            r"authorization[\"']?\s*[:=,]?\s*[\"']?(?:token|bearer)\s+"
            rf"(?=[{_CHARS}:]*[0-9])(?=[{_CHARS}:]*[A-Za-z])([{_CHARS}:]{{16,}})",
            flags=re.IGNORECASE,
        ),
    )


class AnthropicKeyDetector(RegexBasedDetector):
    """An Anthropic API or admin key, wherever it appears — no assignment needed."""

    secret_type = "Anthropic API Key"

    denylist = (re.compile(r"\b(sk-ant-[A-Za-z0-9]{2,8}-[A-Za-z0-9_\-]{20,})"),)


_KITE_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"((?=[A-Za-z0-9]{0,31}[A-Z])(?=[A-Za-z0-9]{0,31}[a-z])(?=[A-Za-z0-9]{0,31}[0-9])"
    r"[A-Za-z0-9]{32})"
    r"(?![A-Za-z0-9_])"
)
_HEX = re.compile(r"[0-9A-Fa-f]+")
_KITE_CONTEXT = re.compile(r"kite|token|access", re.IGNORECASE)
_CONFIG_SUFFIXES = frozenset({".env", ".json", ".yaml", ".yml", ".md", ".toml"})


def _is_config_file(filename: str) -> bool:
    path = PurePath(filename)
    return path.suffix.lower() in _CONFIG_SUFFIXES or path.name.startswith(".env")


class KiteShapedTokenDetector(RegexBasedDetector):
    """A bare 32-character mixed-case alphanumeric value — the shape of a Kite access token.

    Upper, lower and digit all required, and never pure hex, so sha256/sha1/md5 digests (this repo
    is full of them) do not match. Only on a line that mentions kite/token/access, or anywhere in a
    config or prose file (.env .json .yaml .yml .md .toml), where a pasted token has no keyword.
    """

    secret_type = "Kite-shaped Token"

    denylist = (_KITE_SHAPE,)

    def analyze_line(
        self,
        filename: str,
        line: str,
        line_number: int = 0,
        context: CodeSnippet | None = None,
        **kwargs: Any,
    ) -> set[PotentialSecret]:
        if not (_is_config_file(filename) or _KITE_CONTEXT.search(line)):
            return set()
        snippet = cast(CodeSnippet, context)
        found: set[PotentialSecret] = super().analyze_line(
            filename, line, line_number, snippet, **kwargs
        )
        return found

    def analyze_string(self, string: str) -> Generator[str, None, None]:
        for value in super().analyze_string(string):
            if not _HEX.fullmatch(value):
                yield value


# A conninfo value ends at whitespace, a quote, or the punctuation that closes the surrounding
# code or prose; `<value>`, `{pw}`, `%s`, `$PW` and `None` are placeholders, not passwords.
_CONNINFO_VALUE = r"[^\s'\"`,;()<>{}\[\]]+"
_PLACEHOLDER_START = tuple("%$*")
_LITERALS = frozenset({"none", "null", "nil", "true", "false", "redacted"})


def _is_conninfo_value(value: str) -> bool:
    return not value.startswith(_PLACEHOLDER_START) and value.lower() not in _LITERALS


class ConninfoPasswordDetector(RegexBasedDetector):
    """`password=<value>` inside a libpq conninfo string or an env line.

    `psycopg.connect("host=db user=u password=<value> dbname=t")` and `PG=host=db password=<value>`.
    In code files only the quoted-string form counts, so a keyword argument
    (`connect(password=settings.pw)`) is not a finding — that is KeywordDetector's ground.
    """

    secret_type = "Conninfo Password"

    _in_string = re.compile(
        # An opening quote, then only `key=value` pairs, then password= — the conninfo shape. Not
        # "any quote earlier on the line": that also matches the closing quote in
        # `connect(host="db", password=settings.pw)`.
        r"[\"']\s*(?:\w+\s*=\s*[^\s\"']*\s+)*password\s*=\s*(" + _CONNINFO_VALUE + ")",
        re.IGNORECASE,
    )
    _env_line = re.compile(
        r"^\s*(?:export\s+)?[A-Za-z_]\w*\s*=[^'\"\n]*?\bpassword\s*=\s*(" + _CONNINFO_VALUE + ")",
        re.IGNORECASE,
    )
    denylist = (_in_string,)

    def analyze_string(self, string: str) -> Generator[str, None, None]:
        for value in super().analyze_string(string):
            if _is_conninfo_value(value):
                yield value

    def analyze_line(
        self,
        filename: str,
        line: str,
        line_number: int = 0,
        context: CodeSnippet | None = None,
        **kwargs: Any,
    ) -> set[PotentialSecret]:
        snippet = cast(CodeSnippet, context)
        found: set[PotentialSecret] = super().analyze_line(
            filename, line, line_number, snippet, **kwargs
        )
        if PurePath(filename).suffix != ".py":
            for value in filter(_is_conninfo_value, self._env_line.findall(line)):
                found.add(PotentialSecret(self.secret_type, filename, value, line_number))
        return found
