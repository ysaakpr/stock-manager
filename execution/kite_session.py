"""M19.1 · The Kite Connect daily session: login exchange, a 0600 token file, and a small CLI.

Every Kite access token comes from a human login (spec §1): the owner opens the login URL, signs in
with 2FA, and Kite redirects to the app's registered URL with a short-lived ``request_token``. This
module exchanges that token for an ``access_token`` (``POST /session/token`` with
``checksum = sha256(api_key + request_token + api_secret)``) and keeps it, with its expiry, in one
file outside the repo (``Settings.kite_session_file``, by default
``~/.config/smagent/kite-session.json``) at mode 0600. A token expires at the next 06:00 IST
(or on a master logout from Kite web).

Shared by phase A (the read-only portfolio review, `execution.kite_read`) and phase B
(`KiteBroker`).

What it never does:

* store a password or a TOTP seed, or automate the login or the 2FA (NSE/INVG/73992 §8.3.2.1.8);
* call Kite's logout API — that puts the token in a query string, which invariant #13 forbids; the
  token dies at 06:00 IST anyway and ``forget`` deletes the file;
* take a token on argv, put it in a URL, a log line or an exception message, or load a token file
  that other users can read.

CLI (``uv run python -m execution.kite_session <command>``):

* ``login``     print the login URL, read the pasted redirect URL (or the bare request_token)
                from stdin, exchange it and store the session;
* ``set-token`` fallback: read an ``access_token`` from stdin, verify it with
                ``GET /user/profile``, store it;
* ``status``    logged in?, masked user id, expiry in IST — never the token;
* ``forget``    delete the session file.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TextIO
from urllib.parse import parse_qs, urlsplit

import structlog
from pydantic import SecretStr

from dataplatform.clock import IST, Clock, SystemClock
from execution.kite_broker import (
    KITE_API_BASE,
    KITE_API_VERSION,
    KiteApiError,
    KiteMalformedResponseError,
    KiteSessionError,
    LiveKiteTransport,
    _as_mapping,
    _field,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    import httpx

    from dataplatform.config import Settings

__all__ = [
    "KITE_LOGIN_URL",
    "LOGIN_HINT",
    "SESSION_EXPIRY_TIME",
    "SESSION_FILE_MODE",
    "KiteLoginInputError",
    "KiteSession",
    "KiteSessionExchangeError",
    "KiteSessionFileError",
    "LoginNeededError",
    "SessionStore",
    "exchange_request_token",
    "login_checksum",
    "login_url",
    "main",
    "mask_user_id",
    "next_expiry",
    "parse_request_token",
    "session_status",
]

_LOG = structlog.get_logger(__name__)

#: Where the owner signs in. ``v=3`` selects Kite Connect v3; ``api_key`` is the app id, which
#: Kite requires in this URL — it identifies the app and cannot authenticate without the secret.
KITE_LOGIN_URL: Final[str] = "https://kite.zerodha.com/connect/login"
_PATH_SESSION_TOKEN: Final[str] = "/session/token"
#: A Kite access token is valid until 06:00 IST the next morning.
SESSION_EXPIRY_TIME: Final[time] = time(6, 0)
#: The only mode a session file may have: owner read/write, nothing for group or others.
SESSION_FILE_MODE: Final[int] = 0o600
_DIR_MODE: Final[int] = 0o700
#: Kite request tokens and access tokens are short alphanumeric strings.
_TOKEN: Final = re.compile(r"[A-Za-z0-9]{6,128}")
LOGIN_HINT: Final[str] = "run `uv run python -m execution.kite_session login`"


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class LoginNeededError(KiteSessionError):
    """There is no usable session: no file, or the stored token has expired.

    A `KiteSessionError`, so a caller that already handles Kite's own ``TokenException`` handles
    this the same way: stop, and tell the owner to log in.
    """


class KiteSessionFileError(Exception):
    """The session file exists but cannot be trusted: wrong permissions, or not a session.

    The message names the path and the problem, never the file's content.
    """


class KiteLoginInputError(ValueError):
    """What the owner pasted is neither a redirect URL carrying a request_token nor a bare token."""


class KiteSessionExchangeError(KiteSessionError):
    """``POST /session/token`` failed on the network (timeout, connection refused).

    Kite's own API errors come through `LiveKiteTransport._unwrap` as `KiteSessionError` /
    `KiteRequestError`, unchanged.
    """


# ── the session value ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KiteSession:
    """One day's Kite session. ``access_token`` stays a `SecretStr`; its repr is masked.

    ``login_time`` and ``expires_at`` are tz-aware IST datetimes.
    """

    access_token: SecretStr
    user_id: str
    login_time: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("login_time", "expires_at"):
            value: datetime = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"KiteSession.{name} must be tz-aware (IST), got a naive datetime")

    def is_valid(self, clock: Clock) -> bool:
        """True while the token has not reached its 06:00 IST expiry."""
        return clock.now() < self.expires_at

    @property
    def masked_user_id(self) -> str:
        """The Kite user id with all but its first two characters masked (``AB****``)."""
        return mask_user_id(self.user_id)


def mask_user_id(user_id: str) -> str:
    """``AB1234`` → ``AB****``. An id of two characters or fewer is masked entirely."""
    if len(user_id) <= 2:
        return "*" * len(user_id)
    return user_id[:2] + "*" * (len(user_id) - 2)


def next_expiry(login_time: datetime) -> datetime:
    """The first 06:00 IST strictly after ``login_time`` — when Kite expires the token.

    A login at 05:30 IST expires at 06:00 the same morning; a login at 09:00 IST at 06:00 the
    next morning. ``login_time`` must be tz-aware.
    """
    if login_time.tzinfo is None:
        raise ValueError("login_time must be tz-aware")
    local = login_time.astimezone(IST)
    candidate = datetime.combine(local.date(), SESSION_EXPIRY_TIME, tzinfo=IST)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


# ── the login exchange ───────────────────────────────────────────────────────────────────────


def login_url(api_key: SecretStr) -> str:
    """The Kite Connect v3 login URL for this app.

    The api_key is Kite's app identifier and the login page requires it in the query string; it is
    printed to the owner's own terminal and never logged. The api_secret and the tokens never go
    into any URL.
    """
    return f"{KITE_LOGIN_URL}?v=3&api_key={api_key.get_secret_value()}"


def parse_request_token(pasted: str) -> SecretStr:
    """The ``request_token`` from a pasted redirect URL, or the pasted text if it is a bare token.

    Accepts ``http://127.0.0.1/kite/callback?request_token=…&action=login&status=success`` (the
    registered redirect; nothing has to listen there — the token is in the address bar) or just
    the token. A redirect whose ``status`` is not ``success`` is refused. Error messages never
    repeat what was pasted.
    """
    text = pasted.strip()
    if not text:
        raise KiteLoginInputError("nothing was pasted")
    if "://" in text or "?" in text or "request_token" in text:
        query = parse_qs(urlsplit(text).query)
        status = query.get("status", ["success"])[0]
        if status != "success":
            raise KiteLoginInputError(f"the redirect reports status={status!r}, not success")
        tokens = query.get("request_token", [])
        if len(tokens) != 1:
            raise KiteLoginInputError("the pasted URL carries no single request_token parameter")
        text = tokens[0].strip()
    if _TOKEN.fullmatch(text) is None:
        raise KiteLoginInputError("the request_token is not a 6-128 character alphanumeric string")
    return SecretStr(text)


def login_checksum(api_key: SecretStr, request_token: SecretStr, api_secret: SecretStr) -> str:
    """``sha256(api_key + request_token + api_secret)`` as hex — Kite's session checksum."""
    material = (
        api_key.get_secret_value()
        + request_token.get_secret_value()
        + api_secret.get_secret_value()
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def exchange_request_token(
    request_token: SecretStr,
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    client: httpx.Client,
    clock: Clock,
    base_url: str = KITE_API_BASE,
) -> KiteSession:
    """Exchange a request_token for the day's access token (``POST /session/token``).

    What it does: sends the api_key, the request_token and the checksum as a form body with
    ``X-Kite-Version: 3``, parses the envelope with the shared `LiveKiteTransport._unwrap` (money
    as Decimal, ``TokenException`` → `KiteSessionError`), and returns a `KiteSession` stamped with
    the clock's now and the next 06:00 IST.
    What it never does: put any credential in the URL, log one, or chain an httpx exception (whose
    text could carry request details) into the error it raises.
    """
    import httpx  # local: the offline suite injects a MockTransport-backed client

    form = {
        "api_key": api_key.get_secret_value(),
        "request_token": request_token.get_secret_value(),
        "checksum": login_checksum(api_key, request_token, api_secret),
    }
    url = f"{base_url.rstrip('/')}{_PATH_SESSION_TOKEN}"
    try:
        response = client.post(url, data=form, headers={"X-Kite-Version": KITE_API_VERSION})
    except httpx.TimeoutException:
        raise KiteSessionExchangeError("POST /session/token timed out") from None
    except httpx.HTTPError as err:
        raise KiteSessionExchangeError(
            f"POST /session/token failed on the network ({type(err).__name__})"
        ) from None
    data = _as_mapping(
        LiveKiteTransport._unwrap(response.text, response.status_code), where="session/token"
    )
    token = _field(data, "access_token", where="session/token")
    user_id = _field(data, "user_id", where="session/token")
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise KiteMalformedResponseError("session/token: access_token is not a token string")
    if not isinstance(user_id, str) or not user_id:
        raise KiteMalformedResponseError("session/token: user_id is not a non-empty string")
    now = clock.now()
    session = KiteSession(
        access_token=SecretStr(token),
        user_id=user_id,
        login_time=now,
        expires_at=next_expiry(now),
    )
    _LOG.info(
        "kite.session.exchanged",
        user=session.masked_user_id,
        expires_at=session.expires_at.isoformat(),
    )
    return session


# ── the store ────────────────────────────────────────────────────────────────────────────────


class SessionStore:
    """The session file: written 0600 (atomically), refused on load if group/other can read it.

    What it assumes: one owner, one file; the directory is created 0700 if missing.
    What it never does: print, log or put the file's content in an exception message.
    """

    __slots__ = ("_clock", "_path")

    def __init__(self, path: Path, *, clock: Clock) -> None:
        self._path = path.expanduser()
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def save(self, session: KiteSession) -> None:
        """Write ``session`` to the file at mode 0600, replacing any earlier one atomically."""
        self._path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        body = json.dumps(
            {
                "access_token": session.access_token.get_secret_value(),
                "user_id": session.user_id,
                "login_time": session.login_time.astimezone(IST).isoformat(),
                "expires_at": session.expires_at.astimezone(IST).isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")
        tmp = self._path.with_name(self._path.name + ".tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, SESSION_FILE_MODE)
        try:
            os.fchmod(fd, SESSION_FILE_MODE)  # the umask cannot widen it, but be explicit
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp.replace(self._path)
        _LOG.info("kite.session.saved", user=session.masked_user_id)

    def load(self) -> KiteSession | None:
        """The stored session, or ``None`` when there is no file. Expired sessions are returned.

        Raises `KiteSessionFileError` when the file is readable by group or others, is not a
        regular file, or does not hold a session.
        """
        try:
            info = self._path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise KiteSessionFileError(f"{self._path} is not a regular file")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise KiteSessionFileError(
                f"{self._path} has mode {mode:04o}; a Kite session file must be 0600 "
                f"(chmod 600 it, or `forget` and log in again)"
            )
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise KiteSessionFileError(f"{self._path} is not a readable JSON session") from None
        try:
            if not isinstance(raw, dict):
                raise TypeError("not an object")
            token = raw["access_token"]
            user_id = raw["user_id"]
            if not isinstance(token, str) or not isinstance(user_id, str):
                raise TypeError("token and user_id must be strings")
            return KiteSession(
                access_token=SecretStr(token),
                user_id=user_id,
                login_time=datetime.fromisoformat(str(raw["login_time"])),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            )
        except (KeyError, TypeError, ValueError) as err:
            # The error text names the problem class only; the content never leaves the file.
            raise KiteSessionFileError(
                f"{self._path} does not hold a Kite session ({type(err).__name__})"
            ) from None

    def require_valid(self) -> KiteSession:
        """The stored session if it has not expired; `LoginNeededError` otherwise."""
        session = self.load()
        if session is None:
            raise LoginNeededError(f"login needed: no Kite session; {LOGIN_HINT}")
        if not session.is_valid(self._clock):
            raise LoginNeededError(
                f"login needed: the Kite session expired at "
                f"{session.expires_at.astimezone(IST):%Y-%m-%d %H:%M} IST; {LOGIN_HINT}"
            )
        return session

    def forget(self) -> bool:
        """Delete the session file. True if there was one."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        _LOG.info("kite.session.forgotten")
        return True


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Deps:
    """What the CLI needs from the outside world, injectable so the tests stay offline."""

    settings: Settings
    clock: Clock
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    client_factory: Callable[[], httpx.Client]
    verify_token: Callable[[SecretStr, SecretStr], str] | None = None


def _read_secret_line(deps: _Deps, prompt: str) -> str:
    """One line from stdin; without echo when stdin is a terminal. Never from argv."""
    if deps.stdin.isatty():  # pragma: no cover - interactive only
        return getpass.getpass(prompt, stream=deps.stderr)
    deps.stderr.write(prompt)
    deps.stderr.flush()
    return deps.stdin.readline()


def _require(value: SecretStr | None, name: str) -> SecretStr:
    if value is None:
        raise SystemExit(f"{name} is not set: add it to .env (see .env.example)")
    return value


def _store(deps: _Deps) -> SessionStore:
    return SessionStore(deps.settings.kite_session_file, clock=deps.clock)


def _cmd_login(deps: _Deps) -> int:
    api_key = _require(deps.settings.kite_api_key, "KITE_API_KEY")
    api_secret = _require(deps.settings.kite_api_secret, "KITE_API_SECRET")
    deps.stdout.write(
        "Open this URL, log in to Kite (with 2FA), then paste the address you are redirected to\n"
        "(the page itself may show an error; the request_token is in the address bar):\n\n"
        f"  {login_url(api_key)}\n\n"
    )
    deps.stdout.flush()
    pasted = _read_secret_line(deps, "redirect URL or request_token: ")
    try:
        request_token = parse_request_token(pasted)
    except KiteLoginInputError as err:
        deps.stderr.write(f"error: {err}\n")
        return 2
    with deps.client_factory() as client:
        session = exchange_request_token(
            request_token, api_key=api_key, api_secret=api_secret, client=client, clock=deps.clock
        )
    _store(deps).save(session)
    deps.stdout.write(
        f"logged in as {session.masked_user_id}; session valid until "
        f"{session.expires_at.astimezone(IST):%Y-%m-%d %H:%M} IST\n"
    )
    return 0


def _verify_with_profile(api_key: SecretStr, token: SecretStr) -> str:
    """``GET /user/profile`` with ``token``; returns the account's user id. GET only."""
    import httpx

    from execution.kite_read import KiteReader, KiteReadTransport

    with httpx.Client(timeout=15.0) as client:
        reader = KiteReader(KiteReadTransport(api_key=api_key, access_token=token, client=client))
        return reader.profile().user_id


def _cmd_set_token(deps: _Deps) -> int:
    api_key = _require(deps.settings.kite_api_key, "KITE_API_KEY")
    raw = _read_secret_line(deps, "access_token: ").strip()
    if _TOKEN.fullmatch(raw) is None:
        deps.stderr.write("error: that is not a 6-128 character alphanumeric access token\n")
        return 2
    token = SecretStr(raw)
    store = _store(deps)
    verify = deps.verify_token if deps.verify_token is not None else _verify_with_profile
    user_id = verify(api_key, token)
    now = deps.clock.now()
    session = KiteSession(
        access_token=token, user_id=user_id, login_time=now, expires_at=next_expiry(now)
    )
    store.save(session)
    deps.stdout.write(
        f"token stored for {session.masked_user_id}; valid until "
        f"{session.expires_at.astimezone(IST):%Y-%m-%d %H:%M} IST\n"
    )
    return 0


def session_status(store: SessionStore, clock: Clock) -> dict[str, Any]:
    """Logged in?, masked user id, login and expiry times in IST. Never the token."""
    try:
        session = store.load()
    except KiteSessionFileError as err:
        return {"logged_in": False, "problem": str(err), "hint": LOGIN_HINT}
    if session is None:
        return {"logged_in": False, "problem": "no session file", "hint": LOGIN_HINT}
    valid = session.is_valid(clock)
    out: dict[str, Any] = {
        "logged_in": valid,
        "user": session.masked_user_id,
        "login_time_ist": session.login_time.astimezone(IST).isoformat(timespec="seconds"),
        "expires_at_ist": session.expires_at.astimezone(IST).isoformat(timespec="seconds"),
    }
    if not valid:
        out["problem"] = "session expired"
        out["hint"] = LOGIN_HINT
    return out


def _cmd_status(deps: _Deps) -> int:
    status = session_status(_store(deps), deps.clock)
    deps.stdout.write(json.dumps(status, indent=2) + "\n")
    return 0 if status["logged_in"] else 1


def _cmd_forget(deps: _Deps) -> int:
    removed = _store(deps).forget()
    deps.stdout.write("session file deleted\n" if removed else "no session file\n")
    return 0


_COMMANDS: Final[dict[str, Callable[[_Deps], int]]] = {
    "login": _cmd_login,
    "set-token": _cmd_set_token,
    "status": _cmd_status,
    "forget": _cmd_forget,
}


def _default_client() -> httpx.Client:
    import httpx

    return httpx.Client(timeout=15.0)


def main(argv: list[str] | None = None, *, deps: _Deps | None = None) -> int:
    """``python -m execution.kite_session {login,set-token,status,forget}``. No secret on argv."""
    parser = argparse.ArgumentParser(
        prog="kite-session", description="The daily Kite Connect session (login, status, forget)."
    )
    parser.add_argument("command", choices=sorted(_COMMANDS))
    args = parser.parse_args(argv)
    if deps is None:
        from dataplatform.config import get_settings
        from dataplatform.logging import configure_logging

        settings = get_settings()
        clock = SystemClock(settings.tzinfo)
        configure_logging(settings, clock=clock)
        deps = _Deps(
            settings=settings,
            clock=clock,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            client_factory=_default_client,
        )
    try:
        return _COMMANDS[args.command](deps)
    except (KiteApiError, KiteSessionFileError) as err:
        deps.stderr.write(f"error: {err}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
