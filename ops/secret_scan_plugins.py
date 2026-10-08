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


# A name that carries a credential: the keyword anywhere in it, words joined by `_` or `-`
# (KITE_ACCESS_TOKEN, TOKEN_VALUE, x-api-key, X-Api-Key, KITE_PASS, db_password). `pass` only as a
# whole word (not passport/passenger/passthrough/bypass); `token` not as tokenize/tokenizer.
_NAME = r"[\w-]*(?:token(?!i[sz])|secret|api[-_]?key|passw(?:or)?d|(?<![a-z])pass(?![a-z]))[\w-]*"
# A Python annotation, brackets balanced to two levels: `str`, `SecretStr | None`,
# `dict[str, str]`, `Annotated[SecretStr, Field(description="…")]`.
_BRACKET_0 = r"[^\[\]\n]*"
_BRACKET_1 = rf"\[(?:[^\[\]\n]|\[{_BRACKET_0}\])*\]"
_BRACKET_2 = rf"\[(?:[^\[\]\n]|{_BRACKET_1})*\]"
_TYPE = rf"[\w.]+(?:{_BRACKET_2})?"
_ANNOTATION = rf"{_TYPE}(?:\s*\|\s*{_TYPE})*"
# The assignment: `:`/`=`/`:=`, or a Python annotation then `=`. Never `==`, `!=`, `<=`, `>=`.
_ASSIGN = rf"[\"']?\s*(?::\s*{_ANNOTATION}\s*=(?!=)|:=|=(?!=)|:)\s*"
# What the literal may sit in, outermost first, on the same line: a dict literal's first key
# (`{{"kite": "…"}}`), pydantic/dataclass `Field(`/`field(` with an optional `default=`, and
# `SecretStr(`/`SecretBytes(`/`Secret(`/`str(`/`bytes(`.
_WRAPPER = (
    r"(?:\{\s*[\"'][^\"'\n]*[\"']\s*:\s*)?"
    r"(?:(?:Field|field)\(\s*(?:default\s*=\s*)?)?"
    r"(?:(?:SecretStr|SecretBytes|Secret|str|bytes)\(\s*)?"
)
# A Python/JS string prefix: b"…", f"…", r"…", rb"…", u"…".
_PREFIX = r"(?:[bBfFrRuU]{1,2})?"
# An unquoted value that is a reference, not a literal: a dotted chain (`settings.db2_password`,
# `self.access_token_v2`) or a call/subscript (`b64encode(…)`, `x[…]`). Only meaningful in code.
_REFERENCE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?![\w.])|[A-Za-z_]\w*\s*[(\[]")
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".java",
        ".kt",
        ".rb",
        ".rs",
    }
)
_COMMENT_START = re.compile(r"#|//|/\*|^\s*\*")
_QUOTED = re.compile(_NAME + _ASSIGN + _WRAPPER + _PREFIX + r"[\"']" + _VALUE, re.IGNORECASE)
_UNQUOTED = re.compile(_NAME + _ASSIGN + _WRAPPER + rf"(?=[{_CHARS}])" + _VALUE, re.IGNORECASE)


class TokenAssignmentDetector(RegexBasedDetector):
    """A credential assigned or passed: `KITE_ACCESS_TOKEN=…`, `token: …`, `x-api-key: …`,
    `APP=1 KITE_API_SECRET=…`, `# api_secret = …`, `kite.set_access_token("…")`, and the
    pydantic/dataclass shapes `kite_api_secret: str = "…"`, `token: SecretStr = SecretStr("…")`,
    `x: Annotated[str, Field(…)] = "…"`, `x: SecretStr = Field(default="…")`, `x: bytes = b"…"`,
    `x: dict[str, str] = {"kite": "…"}`.

    Any name containing token, secret, api-key/api_key/apikey, password, passwd or a whole-word
    pass, in any syntax, inside a comment or not, plus the call form `…token("<value>")`. A quoted
    value is always a literal. An unquoted value is a literal too, except one exemption: in a code
    file (.py, .js, .ts, …) and **before any comment marker on the line**, an unquoted dotted chain,
    call or subscript is a reference (`password=settings.db2_password.get_secret_value()`). In
    .env, .sh, .md, INI, YAML and every comment, nothing is a reference — `DB_PASSWORD=a.b123…` is
    a value there. Line-based: a value on a later line than its name is a named gap (runbook).
    """

    secret_type = "Credential Assignment"

    denylist = (
        _QUOTED,
        re.compile(r"\w*token\(\s*" + _PREFIX + r"[\"']" + _VALUE, flags=re.IGNORECASE),
    )

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
        code = PurePath(filename).suffix.lower() in _CODE_SUFFIXES
        comment = _COMMENT_START.search(line)
        code_until = comment.start() if comment else len(line)
        for match in _UNQUOTED.finditer(line):
            value = match.group(1)
            is_reference = (
                code
                and match.start(1) < code_until
                and _REFERENCE.match(line, match.start(1)) is not None
            )
            if not is_reference:
                found.add(PotentialSecret(self.secret_type, filename, value, line_number))
        return found


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
