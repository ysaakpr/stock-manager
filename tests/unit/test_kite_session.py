"""M19.1 — the Kite session: login exchange, the 0600 token file, and the CLI (spec §3, I3).

Offline: the ``POST /session/token`` exchange runs against an `httpx.MockTransport` that records
the request and answers with the recorded envelope in ``tests/fixtures/kite_read/``. The clock is
frozen. Every credential here is synthetic.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs

import httpx
import pytest
import structlog
from pydantic import SecretStr

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from execution.kite_broker import KiteSessionError
from execution.kite_session import (
    SESSION_FILE_MODE,
    KiteLoginInputError,
    KiteSession,
    KiteSessionExchangeError,
    KiteSessionFileError,
    LoginNeededError,
    SessionStore,
    _Deps,
    exchange_request_token,
    login_checksum,
    login_url,
    main,
    next_expiry,
    parse_request_token,
)

FIXTURES: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "kite_read"
API_KEY: Final = SecretStr("synthkey")
API_SECRET: Final = SecretStr("synthsecretvalue")
REQUEST_TOKEN: Final = "synthreqtoken01"
ACCESS_TOKEN: Final = "synthtoken0001"  # what session_token.json carries
NOW: Final = datetime(2026, 11, 6, 9, 0, tzinfo=IST)


def _clock(at: datetime = NOW) -> FrozenClock:
    return FrozenClock(at)


def _session(expires: datetime | None = None) -> KiteSession:
    return KiteSession(
        access_token=SecretStr(ACCESS_TOKEN),
        user_id="AB1234",
        login_time=NOW,
        expires_at=expires or next_expiry(NOW),
    )


# ── expiry ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("login", "expiry"),
    [
        (datetime(2026, 11, 6, 9, 0, tzinfo=IST), datetime(2026, 11, 7, 6, 0, tzinfo=IST)),
        (datetime(2026, 11, 6, 5, 30, tzinfo=IST), datetime(2026, 11, 6, 6, 0, tzinfo=IST)),
        (datetime(2026, 11, 6, 6, 0, tzinfo=IST), datetime(2026, 11, 7, 6, 0, tzinfo=IST)),
        (datetime(2026, 11, 6, 23, 59, tzinfo=IST), datetime(2026, 11, 7, 6, 0, tzinfo=IST)),
    ],
)
def test_token_expires_at_the_next_0600_ist(login: datetime, expiry: datetime) -> None:
    assert next_expiry(login) == expiry
    assert next_expiry(login).tzinfo is not None


def test_naive_login_time_is_refused() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        next_expiry(datetime(2026, 11, 6, 9, 0))


# ── pasted input ───────────────────────────────────────────────────────────────────────────


def test_request_token_from_the_redirect_url() -> None:
    url = (
        f"http://127.0.0.1/kite/callback?request_token={REQUEST_TOKEN}&action=login&status=success"
    )
    assert parse_request_token(url + "\n").get_secret_value() == REQUEST_TOKEN


def test_bare_request_token_is_accepted() -> None:
    assert parse_request_token(f"  {REQUEST_TOKEN}  ").get_secret_value() == REQUEST_TOKEN


@pytest.mark.parametrize(
    "pasted",
    [
        "",
        "http://127.0.0.1/kite/callback?action=login&status=success",
        f"http://127.0.0.1/kite/callback?request_token={REQUEST_TOKEN}&status=cancelled",
        "not a token!",
    ],
)
def test_bad_paste_is_refused_without_echoing_it(pasted: str) -> None:
    with pytest.raises(KiteLoginInputError) as info:
        parse_request_token(pasted)
    assert REQUEST_TOKEN not in str(info.value)


def test_login_url_carries_the_api_key_and_v3_only() -> None:
    url = login_url(API_KEY)
    assert url == "https://kite.zerodha.com/connect/login?v=3&api_key=synthkey"
    assert API_SECRET.get_secret_value() not in url


# ── the exchange ───────────────────────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self._body = body
        self._status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, content=self._body)


def test_exchange_posts_the_documented_checksum_and_returns_a_session() -> None:
    recorder = _Recorder((FIXTURES / "session_token.json").read_bytes())
    with httpx.Client(transport=httpx.MockTransport(recorder)) as client:
        session = exchange_request_token(
            SecretStr(REQUEST_TOKEN),
            api_key=API_KEY,
            api_secret=API_SECRET,
            client=client,
            clock=_clock(),
        )
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/session/token"
    assert request.url.query == b""  # nothing secret in the URL
    assert request.headers["X-Kite-Version"] == "3"
    form = parse_qs(request.content.decode())
    expected = hashlib.sha256(
        (API_KEY.get_secret_value() + REQUEST_TOKEN + API_SECRET.get_secret_value()).encode()
    ).hexdigest()
    assert form == {
        "api_key": ["synthkey"],
        "request_token": [REQUEST_TOKEN],
        "checksum": [expected],
    }
    assert login_checksum(API_KEY, SecretStr(REQUEST_TOKEN), API_SECRET) == expected
    # The api_secret itself never goes on the wire, only its checksum.
    assert API_SECRET.get_secret_value().encode() not in request.content
    assert session.access_token.get_secret_value() == ACCESS_TOKEN
    assert session.user_id == "AB1234"
    assert session.expires_at == datetime(2026, 11, 7, 6, 0, tzinfo=IST)
    assert ACCESS_TOKEN not in repr(session)


def test_exchange_maps_a_token_exception_to_a_session_error() -> None:
    recorder = _Recorder((FIXTURES / "error_token.json").read_bytes(), status=403)
    with (
        httpx.Client(transport=httpx.MockTransport(recorder)) as client,
        pytest.raises(KiteSessionError),
    ):
        exchange_request_token(
            SecretStr(REQUEST_TOKEN),
            api_key=API_KEY,
            api_secret=API_SECRET,
            client=client,
            clock=_clock(),
        )


def test_exchange_network_failure_is_typed_and_carries_no_secret() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.content!r}", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(boom)) as client,
        pytest.raises(KiteSessionExchangeError) as info,
    ):
        exchange_request_token(
            SecretStr(REQUEST_TOKEN),
            api_key=API_KEY,
            api_secret=API_SECRET,
            client=client,
            clock=_clock(),
        )
    text = str(info.value)
    assert REQUEST_TOKEN not in text and "synthkey" not in text
    assert info.value.__cause__ is None and info.value.__suppress_context__


# ── the store ──────────────────────────────────────────────────────────────────────────────


def test_saved_file_is_0600_and_round_trips(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "cfg" / "kite-session.json", clock=_clock())
    store.save(_session())
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == SESSION_FILE_MODE
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token.get_secret_value() == ACCESS_TOKEN
    assert loaded.expires_at == next_expiry(NOW)
    assert set(json.loads(store.path.read_text())) == {
        "access_token",
        "user_id",
        "login_time",
        "expires_at",
    }


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_wider_file_is_refused(tmp_path: Path, mode: int) -> None:
    store = SessionStore(tmp_path / "kite-session.json", clock=_clock())
    store.save(_session())
    store.path.chmod(mode)
    with pytest.raises(KiteSessionFileError, match="must be 0600") as info:
        store.load()
    assert ACCESS_TOKEN not in str(info.value)


def test_garbage_file_error_never_repeats_the_content(tmp_path: Path) -> None:
    path = tmp_path / "kite-session.json"
    path.write_text(json.dumps({"access_token": ACCESS_TOKEN}))
    path.chmod(0o600)
    with pytest.raises(KiteSessionFileError) as info:
        SessionStore(path, clock=_clock()).load()
    assert ACCESS_TOKEN not in str(info.value)


def test_require_valid_says_login_needed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "kite-session.json", clock=_clock())
    with pytest.raises(LoginNeededError, match="login needed"):
        store.require_valid()
    store.save(_session())
    assert store.require_valid().user_id == "AB1234"
    later = SessionStore(store.path, clock=_clock(datetime(2026, 11, 7, 6, 0, tzinfo=IST)))
    with pytest.raises(LoginNeededError, match="expired"):
        later.require_valid()


def test_forget_deletes_the_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "kite-session.json", clock=_clock())
    store.save(_session())
    assert store.forget() is True
    assert not store.path.exists()
    assert store.forget() is False


# ── settings ───────────────────────────────────────────────────────────────────────────────


def test_settings_session_file_defaults_outside_the_checkout() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.kite_session_file == Path("~/.config/smagent/kite-session.json").expanduser()
    assert isinstance(settings.kite_api_key, SecretStr | type(None))


def test_settings_refuses_a_session_file_inside_the_repo() -> None:
    from dataplatform.config import REPO_ROOT

    with pytest.raises(ValueError, match="outside the repository"):
        Settings(_env_file=None, kite_session_file=REPO_ROOT / "kite.json")  # type: ignore[call-arg]


# ── CLI ────────────────────────────────────────────────────────────────────────────────────


def _deps(tmp_path: Path, stdin: str, transport: httpx.MockTransport | None = None) -> _Deps:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        kite_api_key=API_KEY,
        kite_api_secret=API_SECRET,
        kite_session_file=tmp_path / "kite-session.json",
    )
    mock = transport or httpx.MockTransport(
        _Recorder((FIXTURES / "session_token.json").read_bytes())
    )
    return _Deps(
        settings=settings,
        clock=_clock(),
        stdin=io.StringIO(stdin),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        client_factory=lambda: httpx.Client(transport=mock),
        verify_token=lambda key, token: "AB1234",
    )


def _out(deps: _Deps) -> str:
    assert isinstance(deps.stdout, io.StringIO) and isinstance(deps.stderr, io.StringIO)
    return deps.stdout.getvalue() + deps.stderr.getvalue()


def test_cli_login_reads_stdin_and_writes_a_0600_session(tmp_path: Path) -> None:
    redirect = f"http://127.0.0.1/kite/callback?request_token={REQUEST_TOKEN}&status=success\n"
    deps = _deps(tmp_path, redirect)
    with structlog.testing.capture_logs() as logs:
        assert main(["login"], deps=deps) == 0
    text = _out(deps)
    assert "https://kite.zerodha.com/connect/login?v=3&api_key=synthkey" in text
    assert "AB****" in text and "AB1234" not in text
    for secret in (ACCESS_TOKEN, REQUEST_TOKEN, API_SECRET.get_secret_value()):
        assert secret not in text
        assert secret not in repr(logs)
    path = tmp_path / "kite-session.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cli_set_token_reads_stdin_never_argv(tmp_path: Path) -> None:
    deps = _deps(tmp_path, ACCESS_TOKEN + "\n")
    assert main(["set-token"], deps=deps) == 0
    assert ACCESS_TOKEN not in _out(deps)
    stored = SessionStore(tmp_path / "kite-session.json", clock=_clock()).load()
    assert stored is not None and stored.access_token.get_secret_value() == ACCESS_TOKEN
    # The parser has no way to take a token on argv at all.
    with pytest.raises(SystemExit):
        main(["set-token", ACCESS_TOKEN], deps=_deps(tmp_path, ""))


def test_cli_status_masks_the_user_and_never_prints_the_token(tmp_path: Path) -> None:
    deps = _deps(tmp_path, "")
    assert main(["status"], deps=deps) == 1  # no session yet
    SessionStore(tmp_path / "kite-session.json", clock=_clock()).save(_session())
    deps = _deps(tmp_path, "")
    assert main(["status"], deps=deps) == 0
    status = json.loads(_out(deps))
    assert status["logged_in"] is True
    assert status["user"] == "AB****"
    assert status["expires_at_ist"] == "2026-11-07T06:00:00+05:30"
    assert ACCESS_TOKEN not in _out(deps)


def test_cli_forget(tmp_path: Path) -> None:
    SessionStore(tmp_path / "kite-session.json", clock=_clock()).save(_session())
    deps = _deps(tmp_path, "")
    assert main(["forget"], deps=deps) == 0
    assert not (tmp_path / "kite-session.json").exists()


def test_module_has_no_logout_and_no_credential_automation() -> None:
    """One POST (the documented exchange), no DELETE (Kite's logout), no password/TOTP anywhere."""
    import ast

    path = Path(__file__).resolve().parents[2] / "execution" / "kite_session.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert calls.count("post") == 1
    assert not {"delete", "put", "request", "patch"} & set(calls)
    names = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {a.arg.lower() for a in ast.walk(tree) if isinstance(a, ast.arg)}
    assert not {n for n in names if "password" in n or "totp" in n or n == "pin"}
    assert not any(k in os.environ for k in ("KITE_PASSWORD", "KITE_TOTP"))
