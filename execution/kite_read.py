"""M19.2 · The read-only Kite Connect client: a GET-only transport and typed, frozen models.

Phase A reads the owner's own Zerodha account and changes nothing in it (spec §4 I1). A Kite access
token carries no scopes — the token that reads holdings can also place orders — so "read-only" is
enforced here, structurally:

* `KiteReadTransport` has exactly one network method, `get`, and a fixed allowlist of read paths
  (`READ_ALLOWLIST`). A path outside it raises `KiteReadNotAllowedError` before a socket opens.
* Nothing in this module (or in `analyst.owner_portfolio`) imports `KiteBroker`, and an AST test
  fails the build on any ``post``/``put``/``delete``/``request`` call in either.

The envelope parsing — money as `Decimal`, ``TokenException`` → `KiteSessionError`, every other
API error → `KiteRequestError` — is `LiveKiteTransport._unwrap`, shared with the broker rather
than copied. On top of it this transport maps httpx timeouts and connection failures to typed
errors and retries a 429 or a 5xx with a bounded backoff.

The transport returns the raw response bytes (`KiteRawResponse`) so a snapshot can keep exactly
what Kite sent; `parse_envelope` and the ``parse_*`` functions turn bytes into models, and
`KiteReader` composes the two for a live read. A field Kite documents but the payload lacks is a
`KiteMalformedResponseError`, never a silent zero.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import SecretStr

from dataplatform.clock import IST
from execution.broker import BrokerError
from execution.kite_broker import (
    KITE_API_BASE,
    KITE_API_VERSION,
    KiteApiError,
    KiteMalformedResponseError,
    KiteRequestError,
    KiteSessionError,
    LiveKiteTransport,
    _as_decimal,
    _as_int,
    _as_list,
    _as_mapping,
    _field,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    import httpx

# The Kite error types are re-exported so a phase-A caller (analyst.owner_portfolio) handles them
# without naming the broker module at all (invariant #5's guard: analyst/ never names a broker).
__all__ = [
    "READ_ALLOWLIST",
    "Endpoint",
    "KiteApiError",
    "KiteConnectionError",
    "KiteFunds",
    "KiteGtt",
    "KiteGttOrder",
    "KiteHolding",
    "KiteMalformedResponseError",
    "KiteMfHolding",
    "KiteMfOrder",
    "KiteMfSip",
    "KiteNetworkError",
    "KiteOrder",
    "KitePosition",
    "KiteProfile",
    "KiteRawResponse",
    "KiteReadNotAllowedError",
    "KiteReadTransport",
    "KiteReader",
    "KiteRequestError",
    "KiteSegmentFunds",
    "KiteSessionError",
    "KiteTimeoutError",
    "KiteTrade",
    "parse_envelope",
    "parse_funds",
    "parse_gtt_triggers",
    "parse_holdings",
    "parse_mf_holdings",
    "parse_mf_orders",
    "parse_mf_sips",
    "parse_orders",
    "parse_positions",
    "parse_profile",
    "parse_trades",
]

_LOG = structlog.get_logger(__name__)


class Endpoint(StrEnum):
    """Every endpoint phase A may read — name (also the snapshot file stem) → path."""

    PROFILE = "/user/profile"
    MARGINS = "/user/margins"
    HOLDINGS = "/portfolio/holdings"
    POSITIONS = "/portfolio/positions"
    ORDERS = "/orders"
    TRADES = "/trades"
    MF_HOLDINGS = "/mf/holdings"
    MF_SIPS = "/mf/sips"
    MF_ORDERS = "/mf/orders"
    GTT_TRIGGERS = "/gtt/triggers"

    @property
    def stem(self) -> str:
        """The snapshot file stem: ``holdings``, ``mf_sips`` …"""
        return self.name.lower()


#: The only paths `KiteReadTransport.get` will send. Exactly the spec's list (§3 M19.2).
READ_ALLOWLIST: Final[frozenset[str]] = frozenset(e.value for e in Endpoint)

#: 429 and 5xx are retried this many times after the first attempt, sleeping these seconds.
_RETRY_BACKOFF: Final[tuple[float, ...]] = (0.5, 1.0, 2.0)


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class KiteReadNotAllowedError(BrokerError):
    """A read of a path outside `READ_ALLOWLIST` was attempted. Nothing was sent.

    Not a `KiteApiError`: Kite was never contacted; the refusal happened here, on purpose.
    """


class KiteNetworkError(KiteApiError):
    """The request never produced a Kite response (closes review gap G11 for reads)."""


class KiteTimeoutError(KiteNetworkError):
    """The GET timed out (connect, read or pool)."""


class KiteConnectionError(KiteNetworkError):
    """The GET failed below HTTP: DNS, refused connection, TLS, a dropped socket."""


# ── the transport ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KiteRawResponse:
    """Exactly what Kite answered to one GET: the path, the HTTP status and the body bytes."""

    path: str
    status_code: int
    body: bytes


class KiteReadTransport:
    """GET-only Kite Connect transport over an injected `httpx.Client`.

    What it does: refuses any path outside `READ_ALLOWLIST` before any I/O; sends
    ``GET <base><path>`` with ``X-Kite-Version: 3`` and the ``token api_key:access_token``
    header, built per call from the `SecretStr`s (the only place they are unwrapped); retries a
    429 or 5xx up to three times with a bounded backoff; maps httpx timeouts and connection
    failures to `KiteTimeoutError` / `KiteConnectionError`.
    What it assumes: ``client`` has a timeout configured.
    What it never does: send any other verb (there is no method for one), log a header, or chain
    an httpx exception into the error it raises.
    """

    __slots__ = ("_access_token", "_api_key", "_base_url", "_client", "_sleep")

    def __init__(
        self,
        *,
        api_key: SecretStr,
        access_token: SecretStr,
        client: httpx.Client,
        base_url: str = KITE_API_BASE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        auth = f"token {self._api_key.get_secret_value()}:{self._access_token.get_secret_value()}"
        return {"X-Kite-Version": KITE_API_VERSION, "Authorization": auth}

    def get(self, path: str, params: Mapping[str, str] | None = None) -> KiteRawResponse:
        """``GET path`` → the raw response. Raises before any I/O if ``path`` is not allowlisted."""
        if path not in READ_ALLOWLIST:
            raise KiteReadNotAllowedError(
                f"{path!r} is not a phase-A read endpoint; allowed: {sorted(READ_ALLOWLIST)}"
            )
        import httpx

        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            try:
                response = self._client.get(url, params=dict(params or {}), headers=self._headers())
            except httpx.TimeoutException:
                raise KiteTimeoutError(f"GET {path} timed out") from None
            except httpx.HTTPError as err:
                raise KiteConnectionError(f"GET {path} failed: {type(err).__name__}") from None
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < len(_RETRY_BACKOFF):
                _LOG.warning(
                    "kite.read.retry", path=path, status=response.status_code, attempt=attempt + 1
                )
                self._sleep(_RETRY_BACKOFF[attempt])
                attempt += 1
                continue
            _LOG.info(
                "kite.read.get", path=path, status=response.status_code, bytes=len(response.content)
            )
            return KiteRawResponse(
                path=path, status_code=response.status_code, body=response.content
            )


def parse_envelope(raw: KiteRawResponse) -> Any:
    """The ``data`` of a Kite envelope (shared `LiveKiteTransport._unwrap`), or its typed error.

    One refinement over `_unwrap`: it treats *any* HTTP 403 as an expired session, but Kite also
    answers 403 with ``PermissionException`` (an endpoint this app may not call, e.g. MF on an
    account without the segment). That is a refusal of one call, not a dead token, so it stays a
    `KiteRequestError` and the rest of the account can still be read.
    """
    try:
        text = raw.body.decode("utf-8")
    except UnicodeDecodeError:
        raise KiteMalformedResponseError(f"{raw.path}: response is not UTF-8") from None
    try:
        return LiveKiteTransport._unwrap(text, raw.status_code)
    except KiteSessionError as err:
        error_type = _error_type(text)
        if error_type is None or error_type == "TokenException":
            raise
        raise KiteRequestError(error_type, str(err), status_code=raw.status_code) from None


def _error_type(text: str) -> str | None:
    """The envelope's ``error_type``, if it is a JSON object that names one."""
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        return None
    value = env.get("error_type") if isinstance(env, dict) else None
    return value if isinstance(value, str) and value else None


# ── value helpers ────────────────────────────────────────────────────────────────────────────


def _str(row: Mapping[str, Any], key: str, *, where: str) -> str:
    value = _field(row, key, where=where)
    if not isinstance(value, str):
        raise KiteMalformedResponseError(f"{where}: {key!r} must be a string, got {value!r}")
    return value


def _opt_str(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    return None if value is None else str(value)


def _money(row: Mapping[str, Any], key: str, *, where: str) -> Decimal:
    return _as_decimal(_field(row, key, where=where), where=f"{where}.{key}")


def _opt_money(row: Mapping[str, Any], key: str, *, where: str) -> Decimal | None:
    value = row.get(key)
    return None if value is None else _as_decimal(value, where=f"{where}.{key}")


def _qty(row: Mapping[str, Any], key: str, *, where: str) -> int:
    return _as_int(_field(row, key, where=where), where=f"{where}.{key}")


def _ist_timestamp(value: Any, *, where: str) -> datetime | None:
    """Kite's ``YYYY-MM-DD HH:MM:SS`` (IST, naive on the wire) → a tz-aware IST datetime."""
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise KiteMalformedResponseError(f"{where}: {value!r} is not a timestamp") from None
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _opt_date(value: Any, *, where: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise KiteMalformedResponseError(f"{where}: {value!r} is not a date") from None


# ── models ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KiteProfile:
    """The account behind the token. Only what a review needs: no email, no name."""

    user_id: str
    broker: str
    exchanges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KiteHolding:
    """One demat holding, keyed by the ISIN Kite carries in the row (invariant #2).

    ``quantity`` is settled; ``t1_quantity`` bought but not yet settled (T+1). The held quantity
    is their sum (`held_quantity`, the G7 lesson). ``used_quantity`` was sold today out of the
    holding; ``collateral_quantity`` is pledged. ``kite_pnl`` and the prices are Kite's own marks.
    ``mtf_quantity`` / ``mtf_average_price`` come from the ``mtf`` block (margin trading); a
    payload without that block leaves them ``None`` — never zero.
    """

    isin: str
    symbol: str
    exchange: str
    quantity: int
    t1_quantity: int
    used_quantity: int
    collateral_quantity: int
    average_price: Decimal
    last_price: Decimal
    close_price: Decimal
    kite_pnl: Decimal
    mtf_quantity: int | None
    mtf_average_price: Decimal | None

    @property
    def held_quantity(self) -> int:
        """Settled plus T+1 shares: what the account owns right now."""
        return self.quantity + self.t1_quantity


@dataclass(frozen=True, slots=True)
class KitePosition:
    """One net position (today's and carried F&O/intraday). Kite rows carry no ISIN."""

    symbol: str
    exchange: str
    product: str
    quantity: int
    overnight_quantity: int
    average_price: Decimal
    last_price: Decimal
    close_price: Decimal
    pnl: Decimal
    buy_quantity: int
    sell_quantity: int


@dataclass(frozen=True, slots=True)
class KiteSegmentFunds:
    """One segment's margin statement. ``net`` is Kite's available margin."""

    enabled: bool
    net: Decimal
    cash: Decimal
    live_balance: Decimal
    opening_balance: Decimal | None
    collateral: Decimal | None
    debits: Decimal


@dataclass(frozen=True, slots=True)
class KiteFunds:
    """``GET /user/margins``: the equity and commodity segments."""

    equity: KiteSegmentFunds
    commodity: KiteSegmentFunds | None


@dataclass(frozen=True, slots=True)
class KiteOrder:
    """One order from today's order book (Kite returns today only)."""

    order_id: str
    status: str
    symbol: str
    exchange: str
    transaction_type: str
    order_type: str
    product: str
    variety: str
    quantity: int
    filled_quantity: int
    pending_quantity: int
    price: Decimal
    average_price: Decimal
    trigger_price: Decimal
    order_timestamp: datetime | None
    status_message: str | None
    tag: str | None


@dataclass(frozen=True, slots=True)
class KiteTrade:
    """One fill from today's tradebook."""

    trade_id: str
    order_id: str
    symbol: str
    exchange: str
    transaction_type: str
    product: str
    quantity: int
    average_price: Decimal
    fill_timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class KiteMfHolding:
    """One mutual-fund folio. Kite's MF ``tradingsymbol`` *is* the scheme's ISIN."""

    isin: str
    folio: str | None
    fund: str
    quantity: Decimal
    average_price: Decimal
    last_price: Decimal
    last_price_date: date | None
    pnl: Decimal


@dataclass(frozen=True, slots=True)
class KiteMfSip:
    """One standing SIP."""

    sip_id: str
    isin: str
    fund: str
    status: str
    frequency: str
    instalment_amount: Decimal
    completed_instalments: int
    next_instalment: date | None


@dataclass(frozen=True, slots=True)
class KiteMfOrder:
    """One recent MF order."""

    order_id: str
    isin: str
    fund: str
    transaction_type: str
    status: str
    amount: Decimal
    quantity: Decimal
    order_timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class KiteGttOrder:
    """One leg a GTT would place when triggered."""

    transaction_type: str
    quantity: int
    price: Decimal
    order_type: str
    product: str


@dataclass(frozen=True, slots=True)
class KiteGtt:
    """One standing GTT trigger (good-till-triggered)."""

    trigger_id: int
    trigger_type: str
    status: str
    symbol: str
    exchange: str
    trigger_values: tuple[Decimal, ...]
    last_price: Decimal
    orders: tuple[KiteGttOrder, ...]
    created_at: datetime | None
    expires_at: datetime | None


# ── parsers (payload ``data`` → models) ──────────────────────────────────────────────────────


def parse_profile(data: Any) -> KiteProfile:
    row = _as_mapping(data, where="profile")
    exchanges = row.get("exchanges") or []
    return KiteProfile(
        user_id=_str(row, "user_id", where="profile"),
        broker=str(row.get("broker") or ""),
        exchanges=tuple(str(e) for e in _as_list(exchanges, where="profile.exchanges")),
    )


def parse_holdings(data: Any) -> tuple[KiteHolding, ...]:
    out: list[KiteHolding] = []
    for i, item in enumerate(_as_list(data, where="holdings")):
        where = f"holdings[{i}]"
        row = _as_mapping(item, where=where)
        mtf_raw = row.get("mtf")
        mtf = None if mtf_raw is None else _as_mapping(mtf_raw, where=f"{where}.mtf")
        out.append(
            KiteHolding(
                isin=_str(row, "isin", where=where),
                symbol=_str(row, "tradingsymbol", where=where),
                exchange=_str(row, "exchange", where=where),
                quantity=_qty(row, "quantity", where=where),
                t1_quantity=_qty(row, "t1_quantity", where=where),
                used_quantity=_qty(row, "used_quantity", where=where),
                collateral_quantity=_qty(row, "collateral_quantity", where=where),
                average_price=_money(row, "average_price", where=where),
                last_price=_money(row, "last_price", where=where),
                close_price=_money(row, "close_price", where=where),
                kite_pnl=_money(row, "pnl", where=where),
                mtf_quantity=None if mtf is None else _qty(mtf, "quantity", where=f"{where}.mtf"),
                mtf_average_price=None
                if mtf is None
                else _money(mtf, "average_price", where=f"{where}.mtf"),
            )
        )
    return tuple(out)


def parse_positions(data: Any) -> tuple[KitePosition, ...]:
    """The ``net`` positions (``day`` is the same rows restricted to today)."""
    body = _as_mapping(data, where="positions")
    out: list[KitePosition] = []
    for i, item in enumerate(_as_list(_field(body, "net", where="positions"), where="net")):
        where = f"positions.net[{i}]"
        row = _as_mapping(item, where=where)
        out.append(
            KitePosition(
                symbol=_str(row, "tradingsymbol", where=where),
                exchange=_str(row, "exchange", where=where),
                product=_str(row, "product", where=where),
                quantity=_qty(row, "quantity", where=where),
                overnight_quantity=_qty(row, "overnight_quantity", where=where),
                average_price=_money(row, "average_price", where=where),
                last_price=_money(row, "last_price", where=where),
                close_price=_money(row, "close_price", where=where),
                pnl=_money(row, "pnl", where=where),
                buy_quantity=_qty(row, "buy_quantity", where=where),
                sell_quantity=_qty(row, "sell_quantity", where=where),
            )
        )
    return tuple(out)


def _segment(data: Any, *, where: str) -> KiteSegmentFunds:
    seg = _as_mapping(data, where=where)
    available = _as_mapping(_field(seg, "available", where=where), where=f"{where}.available")
    utilised = _as_mapping(_field(seg, "utilised", where=where), where=f"{where}.utilised")
    enabled = seg.get("enabled", True)
    return KiteSegmentFunds(
        enabled=bool(enabled),
        net=_money(seg, "net", where=where),
        cash=_money(available, "cash", where=f"{where}.available"),
        live_balance=_money(available, "live_balance", where=f"{where}.available"),
        opening_balance=_opt_money(available, "opening_balance", where=f"{where}.available"),
        collateral=_opt_money(available, "collateral", where=f"{where}.available"),
        debits=_money(utilised, "debits", where=f"{where}.utilised"),
    )


def parse_funds(data: Any) -> KiteFunds:
    body = _as_mapping(data, where="margins")
    commodity = body.get("commodity")
    return KiteFunds(
        equity=_segment(_field(body, "equity", where="margins"), where="margins.equity"),
        commodity=None if commodity is None else _segment(commodity, where="margins.commodity"),
    )


def parse_orders(data: Any) -> tuple[KiteOrder, ...]:
    out: list[KiteOrder] = []
    for i, item in enumerate(_as_list(data, where="orders")):
        where = f"orders[{i}]"
        row = _as_mapping(item, where=where)
        out.append(
            KiteOrder(
                order_id=str(_field(row, "order_id", where=where)),
                status=_str(row, "status", where=where),
                symbol=_str(row, "tradingsymbol", where=where),
                exchange=_str(row, "exchange", where=where),
                transaction_type=_str(row, "transaction_type", where=where),
                order_type=_str(row, "order_type", where=where),
                product=_str(row, "product", where=where),
                variety=_str(row, "variety", where=where),
                quantity=_qty(row, "quantity", where=where),
                filled_quantity=_qty(row, "filled_quantity", where=where),
                pending_quantity=_qty(row, "pending_quantity", where=where),
                price=_money(row, "price", where=where),
                average_price=_money(row, "average_price", where=where),
                trigger_price=_money(row, "trigger_price", where=where),
                order_timestamp=_ist_timestamp(row.get("order_timestamp"), where=where),
                status_message=_opt_str(row, "status_message"),
                tag=_opt_str(row, "tag"),
            )
        )
    return tuple(out)


def parse_trades(data: Any) -> tuple[KiteTrade, ...]:
    out: list[KiteTrade] = []
    for i, item in enumerate(_as_list(data, where="trades")):
        where = f"trades[{i}]"
        row = _as_mapping(item, where=where)
        out.append(
            KiteTrade(
                trade_id=str(_field(row, "trade_id", where=where)),
                order_id=str(_field(row, "order_id", where=where)),
                symbol=_str(row, "tradingsymbol", where=where),
                exchange=_str(row, "exchange", where=where),
                transaction_type=_str(row, "transaction_type", where=where),
                product=_str(row, "product", where=where),
                quantity=_qty(row, "quantity", where=where),
                average_price=_money(row, "average_price", where=where),
                fill_timestamp=_ist_timestamp(row.get("fill_timestamp"), where=where),
            )
        )
    return tuple(out)


def parse_mf_holdings(data: Any) -> tuple[KiteMfHolding, ...]:
    out: list[KiteMfHolding] = []
    for i, item in enumerate(_as_list(data, where="mf_holdings")):
        where = f"mf_holdings[{i}]"
        row = _as_mapping(item, where=where)
        out.append(
            KiteMfHolding(
                isin=_str(row, "tradingsymbol", where=where),
                folio=_opt_str(row, "folio"),
                fund=_str(row, "fund", where=where),
                quantity=_money(row, "quantity", where=where),
                average_price=_money(row, "average_price", where=where),
                last_price=_money(row, "last_price", where=where),
                last_price_date=_opt_date(row.get("last_price_date"), where=where),
                pnl=_money(row, "pnl", where=where),
            )
        )
    return tuple(out)


def parse_mf_sips(data: Any) -> tuple[KiteMfSip, ...]:
    out: list[KiteMfSip] = []
    for i, item in enumerate(_as_list(data, where="mf_sips")):
        where = f"mf_sips[{i}]"
        row = _as_mapping(item, where=where)
        out.append(
            KiteMfSip(
                sip_id=str(_field(row, "sip_id", where=where)),
                isin=_str(row, "tradingsymbol", where=where),
                fund=_str(row, "fund", where=where),
                status=_str(row, "status", where=where),
                frequency=_str(row, "frequency", where=where),
                instalment_amount=_money(row, "instalment_amount", where=where),
                completed_instalments=_qty(row, "completed_instalments", where=where),
                next_instalment=_opt_date(row.get("next_instalment"), where=where),
            )
        )
    return tuple(out)


def parse_mf_orders(data: Any) -> tuple[KiteMfOrder, ...]:
    out: list[KiteMfOrder] = []
    for i, item in enumerate(_as_list(data, where="mf_orders")):
        where = f"mf_orders[{i}]"
        row = _as_mapping(item, where=where)
        out.append(
            KiteMfOrder(
                order_id=str(_field(row, "order_id", where=where)),
                isin=_str(row, "tradingsymbol", where=where),
                fund=_str(row, "fund", where=where),
                transaction_type=_str(row, "transaction_type", where=where),
                status=_str(row, "status", where=where),
                amount=_money(row, "amount", where=where),
                quantity=_money(row, "quantity", where=where),
                order_timestamp=_ist_timestamp(row.get("order_timestamp"), where=where),
            )
        )
    return tuple(out)


def parse_gtt_triggers(data: Any) -> tuple[KiteGtt, ...]:
    out: list[KiteGtt] = []
    for i, item in enumerate(_as_list(data, where="gtt")):
        where = f"gtt[{i}]"
        row = _as_mapping(item, where=where)
        condition = _as_mapping(_field(row, "condition", where=where), where=f"{where}.condition")
        legs: list[KiteGttOrder] = []
        for j, leg in enumerate(_as_list(_field(row, "orders", where=where), where=where)):
            lw = f"{where}.orders[{j}]"
            lrow = _as_mapping(leg, where=lw)
            legs.append(
                KiteGttOrder(
                    transaction_type=_str(lrow, "transaction_type", where=lw),
                    quantity=_qty(lrow, "quantity", where=lw),
                    price=_money(lrow, "price", where=lw),
                    order_type=_str(lrow, "order_type", where=lw),
                    product=_str(lrow, "product", where=lw),
                )
            )
        triggers = _as_list(
            _field(condition, "trigger_values", where=f"{where}.condition"), where=where
        )
        out.append(
            KiteGtt(
                trigger_id=_qty(row, "id", where=where),
                trigger_type=_str(row, "type", where=where),
                status=_str(row, "status", where=where),
                symbol=_str(condition, "tradingsymbol", where=f"{where}.condition"),
                exchange=_str(condition, "exchange", where=f"{where}.condition"),
                trigger_values=tuple(
                    _as_decimal(v, where=f"{where}.trigger_values") for v in triggers
                ),
                last_price=_money(condition, "last_price", where=f"{where}.condition"),
                orders=tuple(legs),
                created_at=_ist_timestamp(row.get("created_at"), where=where),
                expires_at=_ist_timestamp(row.get("expires_at"), where=where),
            )
        )
    return tuple(out)


# ── the reader ───────────────────────────────────────────────────────────────────────────────


class KiteReader:
    """Typed reads of the owner's account over a `KiteReadTransport`. GET only, by construction.

    Each method sends one allowlisted GET and parses the envelope into frozen models. `raw`
    returns the untouched bytes for a snapshot.
    """

    __slots__ = ("_transport",)

    def __init__(self, transport: KiteReadTransport) -> None:
        self._transport = transport

    def raw(self, endpoint: Endpoint) -> KiteRawResponse:
        return self._transport.get(endpoint.value)

    def _data(self, endpoint: Endpoint) -> Any:
        return parse_envelope(self.raw(endpoint))

    def profile(self) -> KiteProfile:
        return parse_profile(self._data(Endpoint.PROFILE))

    def funds(self) -> KiteFunds:
        return parse_funds(self._data(Endpoint.MARGINS))

    def holdings(self) -> tuple[KiteHolding, ...]:
        return parse_holdings(self._data(Endpoint.HOLDINGS))

    def positions(self) -> tuple[KitePosition, ...]:
        return parse_positions(self._data(Endpoint.POSITIONS))

    def orders(self) -> tuple[KiteOrder, ...]:
        return parse_orders(self._data(Endpoint.ORDERS))

    def trades(self) -> tuple[KiteTrade, ...]:
        return parse_trades(self._data(Endpoint.TRADES))

    def mf_holdings(self) -> tuple[KiteMfHolding, ...]:
        return parse_mf_holdings(self._data(Endpoint.MF_HOLDINGS))

    def mf_sips(self) -> tuple[KiteMfSip, ...]:
        return parse_mf_sips(self._data(Endpoint.MF_SIPS))

    def mf_orders(self) -> tuple[KiteMfOrder, ...]:
        return parse_mf_orders(self._data(Endpoint.MF_ORDERS))

    def gtt_triggers(self) -> tuple[KiteGtt, ...]:
        return parse_gtt_triggers(self._data(Endpoint.GTT_TRIGGERS))
