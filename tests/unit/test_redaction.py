"""`dataplatform.redaction.mask_secrets`: the credential shapes it masks, and the text it leaves be.

Every credential-shaped value here is built at runtime from a seed — a prefix split across string
pieces plus a hash-derived body — so no literal in this file looks like a key to the repo's own
secret scan, and none of them is a real credential (invariant #13).
"""

from __future__ import annotations

import base64
import hashlib
import json
import string

import pytest

from dataplatform.redaction import MASK, mask_secrets

_ALNUM = string.ascii_letters + string.digits


def _body(seed: str, length: int, alphabet: str = _ALNUM) -> str:
    """A deterministic run of `alphabet` characters, mixed case and digits for the default one."""
    out = ""
    counter = 0
    while len(out) < length:
        digest = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        out += "".join(alphabet[b % len(alphabet)] for b in digest)
        counter += 1
    # Pin one of each class so a short alphabet draw can never come out single-case.
    return ("aZ9" + out)[:length] if alphabet == _ALNUM else out[:length]


def _b64url(document: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(document).encode()).rstrip(b"=").decode()


def _jwt() -> str:
    header = _b64url({"alg": "HS256", "typ": "JWT"})
    claims = _b64url({"sub": _body("jwt-sub", 12), "iat": 1700000000})
    return f"{header}.{claims}.{_body('jwt-sig', 43)}"


# (label, the value as it would appear in text). The prefixes are concatenated so the source never
# carries one whole.
_PREFIXED = [
    ("google", "AI" + "za" + _body("google", 35)),
    ("stripe secret", "sk" + "_live_" + _body("stripe", 24)),
    ("stripe restricted", "rk" + "_live_" + _body("stripe-rk", 24)),
    ("stripe test", "sk" + "_test_" + _body("stripe-test", 24)),
    ("gitlab", "gl" + "pat-" + _body("gitlab", 20)),
    ("hugging face", "hf" + "_" + _body("hf", 34)),
    ("npm", "np" + "m_" + _body("npm", 36)),
    ("jwt", _jwt()),
]


@pytest.mark.parametrize(("label", "value"), _PREFIXED, ids=[label for label, _ in _PREFIXED])
def test_a_prefix_identified_token_is_masked_in_prose(label: str, value: str) -> None:
    text = f"the {label} credential is {value}, see above"
    masked = mask_secrets(text)
    assert value not in masked
    assert masked == f"the {label} credential is {MASK}, see above"


@pytest.mark.parametrize(
    "name", ["api_key", "apiKey", "password", "client_secret", "access_token", "Authorization"]
)
def test_a_json_quoted_pair_has_its_value_masked_and_its_shape_kept(name: str) -> None:
    secret = _body(f"json-{name}", 28)
    document = json.dumps({name: secret, "isin": "INE002A01018"})
    masked = mask_secrets(document)
    assert secret not in masked
    assert json.loads(masked) == {name: MASK, "isin": "INE002A01018"}


def test_a_python_repr_pair_and_an_escaped_quote_inside_the_value_are_masked_whole() -> None:
    secret = _body("repr", 20)
    assert mask_secrets(f"{{'token': '{secret}'}}") == f"{{'token': '{MASK}'}}"
    escaped = json.dumps({"password": f'{secret}"{secret}'})
    masked = mask_secrets(escaped)
    assert secret not in masked
    assert json.loads(masked) == {"password": MASK}


def test_a_slack_webhook_url_loses_its_path() -> None:
    path = f"T{_body('t', 8).upper()}/B{_body('b', 8).upper()}/{_body('slack', 24)}"
    url = "https://hooks.slack.com/" + "services/" + path
    masked = mask_secrets(f"posting to {url} failed")
    assert path not in masked
    assert masked == f"posting to https://hooks.slack.com/services/{MASK} failed"


def test_a_discord_webhook_url_loses_its_path() -> None:
    path = f"{_body('id', 18, string.digits)}/{_body('discord', 68)}"
    masked = mask_secrets("https://discord.com/api/" + "webhooks/" + path)
    assert path not in masked
    assert masked == f"https://discord.com/api/webhooks/{MASK}"


def test_a_token_shaped_path_segment_is_masked_on_any_host() -> None:
    token = _body("path-token", 32)
    masked = mask_secrets(f"GET https://api.example.com/v1/{token}/send returned 404")
    assert token not in masked
    assert masked == f"GET https://api.example.com/v1/{MASK}/send returned 404"


@pytest.mark.parametrize(
    "url",
    [
        # A slug: separators, however long and mixed.
        "https://filingreader.com/news-wire/mumbai/2026-06-12/Finolex-Cables-Officer-Resigns-2026",
        # A numeric id and a lower-case hex digest.
        "https://economictimes.indiatimes.com/markets/articleshow/112233445566778899.cms",
        "https://github.com/org/repo/commit/" + hashlib.sha1(b"commit").hexdigest(),
        # Short mixed-case segments.
        "https://trendlyne.com/equity/bulk-block-deals/IDEAOPT/3550/kretto-syscon-ltd/",
        "https://www.nseindia.com/resources/exchange-communication-holidays",
    ],
)
def test_an_ordinary_url_path_is_left_alone(url: str) -> None:
    assert mask_secrets(url, drop_url_queries=False) == url


def test_ordinary_market_prose_is_untouched() -> None:
    text = (
        "Reliance Industries (INE002A01018) closed at ₹2,945.50; the board approved a 1:1 bonus.\n"
        'Token spend: 8 in, 840 out. A JSON row: {"isin": "INE467B01029", "close": "3500"}.'
    )
    assert mask_secrets(text) == text


def test_masking_is_idempotent_on_every_new_shape() -> None:
    once = mask_secrets(
        " ".join(value for _, value in _PREFIXED)
        + json.dumps({"api_key": _body("idem", 20)})
        + " https://hooks.slack.com/services/"
        + _body("idem-hook", 30)
    )
    assert mask_secrets(once) == once


def test_a_youtube_channel_id_in_a_scraped_page_is_not_a_token() -> None:
    """The one false positive the fixture sweep found at a 24-character floor (RBI's home page)."""
    url = "https://www.youtube.com/channel/UC" + _body("yt", 22)
    assert mask_secrets(url) == url
