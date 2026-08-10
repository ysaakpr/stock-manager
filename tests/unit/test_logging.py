"""Structured logging renders what the dashboard and the alerting rules read (§8.1).

The sync dashboard, the failure-streak alert and every post-mortem query fields, not prose, so an
event that loses its `source` or its `logical_date` is a defect even though the line still looks
fine to a human. These tests assert the shape of the output, including that the timestamp comes
from the injected clock (B10) rather than the host.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from pydantic import BaseModel

from dataplatform.clock import IST, FrozenClock
from dataplatform.logging import (
    _HttpxUrlRedactor,
    _redact_known_secret_shapes,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    log_context,
)
from tests.conftest import SettingsLoader

CLOSE = datetime(2026, 8, 7, 15, 30, tzinfo=IST)

#: Configures logging onto a buffer with a frozen clock, and hands back the buffer.
Configure = Callable[..., io.StringIO]


@pytest.fixture(autouse=True)
def reset_structlog() -> Iterator[None]:
    """`configure_logging` is process-wide state; no test may leak it into the next one."""
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


@pytest.fixture
def configured(load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch) -> Configure:
    def configure(*, app_env: str = "prod", log_level: str = "INFO") -> io.StringIO:
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv("LOG_LEVEL", log_level)
        stream = io.StringIO()
        configure_logging(load_settings(None), clock=FrozenClock(CLOSE), stream=stream)
        return stream

    return configure


def emitted(stream: io.StringIO) -> list[dict[str, Any]]:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_prod_emits_one_json_object_per_event(configured: Configure) -> None:
    stream = configured()

    get_logger("ingest").info("bhavcopy_fetched", source="nse_bhavcopy", rows=2143)

    (event,) = emitted(stream)
    assert event["event"] == "bhavcopy_fetched"
    assert event["level"] == "info"
    assert event["source"] == "nse_bhavcopy"
    assert event["rows"] == 2143


def test_the_timestamp_comes_from_the_injected_clock(configured: Configure) -> None:
    """A replayed run's log must be as reproducible as its journal."""
    stream = configured()

    get_logger().info("replayed")

    (event,) = emitted(stream)
    assert event["timestamp"] == "2026-08-07T15:30:00.000+05:30"


def test_bound_context_rides_along_with_every_event(configured: Configure) -> None:
    stream = configured()
    log = get_logger("ingest")

    bind_context(run_id="wave-7", logical_date="2026-08-07")
    log.info("fetch_started")
    log.warning("retrying", attempt=2)
    clear_context()
    log.info("done")

    started, retrying, done = emitted(stream)
    assert started["run_id"] == retrying["run_id"] == "wave-7"
    assert retrying["logical_date"] == "2026-08-07"
    assert retrying["attempt"] == 2
    assert "run_id" not in done


def test_log_context_unbinds_even_when_the_block_raises(configured: Configure) -> None:
    stream = configured()
    log = get_logger()

    with pytest.raises(RuntimeError), log_context(source="nse_bhavcopy"):
        log.error("fetch_failed", status=403)
        raise RuntimeError("403 streak")
    log.info("next_source")

    failed, next_source = emitted(stream)
    assert failed["source"] == "nse_bhavcopy"
    assert "source" not in next_source


def test_the_configured_level_filters_events(configured: Configure) -> None:
    stream = configured(log_level="WARNING")
    log = get_logger()

    log.info("ignored")
    log.warning("kept")

    assert [event["event"] for event in emitted(stream)] == ["kept"]


def test_dev_renders_for_humans_rather_than_for_a_parser(configured: Configure) -> None:
    stream = configured(app_env="dev")

    get_logger().info("bhavcopy_fetched", source="nse_bhavcopy")

    line = stream.getvalue().strip()
    assert "bhavcopy_fetched" in line
    assert "source" in line
    assert "\x1b[" not in line  # no colour codes on a non-terminal
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_an_exception_is_rendered_as_structured_data_in_prod(configured: Configure) -> None:
    """A traceback squeezed into a message string is not queryable; JSON mode keeps the fields."""
    stream = configured()

    try:
        raise ValueError("checksum mismatch")
    except ValueError:
        get_logger().exception("l0_verify_failed")

    (event,) = emitted(stream)
    assert event["level"] == "error"
    assert event["exception"][0]["exc_type"] == "ValueError"


def test_show_locals_in_tracebacks_is_disabled() -> None:
    """The one flag standing between a frame's plaintext secret and a log line (invariant #13).

    `structlog.processors.dict_tracebacks` and `structlog.dev.ConsoleRenderer`'s default exception
    formatter both hardcode `show_locals=True` — this pins the module-level override so a future
    edit that goes back to the convenience default is caught here, not in production.
    """
    from dataplatform import logging as logging_module

    assert logging_module.SHOW_LOCALS_IN_TRACEBACKS is False
    assert logging_module._JSON_EXCEPTION_TRANSFORMER.show_locals is False
    assert logging_module._CONSOLE_EXCEPTION_FORMATTER.show_locals is False


def _raise_with_a_secret_in_frame_locals(secret_value: str) -> None:
    """A stand-in for `get_secret_value()` called just above a failure — a live plaintext
    credential sitting in exactly the frame a traceback renderer would show.

    Takes the secret as an argument rather than assigning a literal: a real credential is never a
    string literal in source, so a test that hardcodes one there would also (harmlessly) show up
    in Rich's source-context panel and prove nothing about `show_locals` specifically.
    """
    alert_smtp_password = secret_value  # noqa: F841 - the point is that it exists, unused
    raise ValueError("checksum mismatch")


def test_a_secret_in_exception_frame_locals_never_reaches_the_prod_log(
    configured: Configure,
) -> None:
    stream = configured()
    secret = "".join(["s3cret-local-", "9f2c"])

    try:
        _raise_with_a_secret_in_frame_locals(secret)
    except ValueError:
        get_logger().exception("l0_verify_failed")

    assert secret not in stream.getvalue()


def test_a_secret_in_exception_frame_locals_never_reaches_the_dev_log(
    configured: Configure,
) -> None:
    stream = configured(app_env="dev")
    secret = "".join(["s3cret-local-", "9f2c"])

    try:
        _raise_with_a_secret_in_frame_locals(secret)
    except ValueError:
        get_logger().exception("l0_verify_failed")

    assert secret not in stream.getvalue()


def test_a_dsn_password_reaching_a_log_field_is_redacted(configured: Configure) -> None:
    """Defense in depth: even if a caller logs a DSN outright, the password never lands."""
    stream = configured()

    get_logger().error(
        "db_connect_failed", dsn="postgresql://trading:s3cret-db-9f2c@localhost:5433/trading"
    )

    (event,) = emitted(stream)
    assert "s3cret-db-9f2c" not in stream.getvalue()
    assert event["dsn"] == "postgresql://trading:***REDACTED***@localhost:5433/trading"


def test_a_telegram_token_reaching_a_log_field_is_redacted(configured: Configure) -> None:
    """Defense in depth for the exact shape `TelegramConfig.send_message_url` produces."""
    stream = configured()
    token = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"  # 35 chars, matches the Bot API's token shape

    get_logger().error(
        "telegram_delivery_failed", url=f"https://api.telegram.org/bot12345678:{token}/sendMessage"
    )

    (event,) = emitted(stream)
    assert token not in stream.getvalue()
    assert event["url"] == "https://api.telegram.org/bot***REDACTED***/sendMessage"


#: httpx's own "HTTP Request: %s %s ..." call site, verified by reading httpx._client's source —
#: the second positional arg is the request's `httpx.URL` object, not a `str`.
def _httpx_request_log_record(url: httpx.URL) -> logging.LogRecord:
    return logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s"',
        ("POST", url, "HTTP/1.1", 401, "Unauthorized"),
        None,
    )


def test_the_httpx_filter_redacts_a_telegram_token_carried_by_a_url_object() -> None:
    """httpx passes the request URL as its own `httpx.URL` object, not a `str` — only converted
    to text when the record is finally formatted, well after any filter has run. A naive
    `isinstance(arg, str)` check misses this entirely and the token reaches the formatted line
    unredacted; this is exactly the case that broke on the first attempt at this filter."""
    token = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"
    url = httpx.URL(f"https://api.telegram.org/bot12345678:{token}/sendMessage")
    record = _httpx_request_log_record(url)

    assert _HttpxUrlRedactor().filter(record) is True

    assert token not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_the_httpx_filter_leaves_non_matching_arguments_untouched() -> None:
    """A numeric `%d` argument (the status code) must stay numeric, or formatting breaks; a
    method string with nothing to redact must come through byte-for-byte."""
    record = _httpx_request_log_record(httpx.URL("https://nsearchives.nseindia.com/bhavcopy.zip"))

    _HttpxUrlRedactor().filter(record)

    method, url, _http_version, status_code, reason = record.args  # type: ignore[misc]
    assert method == "POST"
    assert str(url) == "https://nsearchives.nseindia.com/bhavcopy.zip"
    assert status_code == 401  # still an int, or "%d" % "401" raises TypeError
    assert reason == "Unauthorized"


def test_the_httpx_logger_is_filtered_end_to_end_after_configure_logging(
    configured: Configure,
) -> None:
    """The concrete production path: something later sets the httpx logger's level to INFO and
    attaches a handler (a future entrypoint, a test, a REPL) — configure_logging must have
    already made that safe by the time it happens, regardless of when."""
    configured()
    httpx_stream = io.StringIO()
    httpx_logger = logging.getLogger("httpx")
    handler = logging.StreamHandler(httpx_stream)
    httpx_logger.addHandler(handler)
    previous_level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    token = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"

    try:
        httpx_logger.info(
            'HTTP Request: %s %s "%s %d %s"',
            "POST",
            httpx.URL(f"https://api.telegram.org/bot12345678:{token}/sendMessage"),
            "HTTP/1.1",
            401,
            "Unauthorized",
        )
    finally:
        httpx_logger.removeHandler(handler)
        httpx_logger.setLevel(previous_level)

    assert token not in httpx_stream.getvalue()


def test_configure_logging_does_not_attach_the_httpx_filter_twice(configured: Configure) -> None:
    """`configure_logging` is called once per test in this module (and per real entrypoint call
    too); the filter must not accumulate one copy of itself per call."""
    configured()
    configured()

    httpx_filters = [
        f for f in logging.getLogger("httpx").filters if isinstance(f, _HttpxUrlRedactor)
    ]
    assert len(httpx_filters) == 1


# ── denylist widening: shapes an earlier review found had zero coverage ────────────────────────


def test_a_telegram_token_with_a_body_longer_than_35_chars_is_redacted() -> None:
    """The original regex hard-coded an exact 35-char body; a real token 36+ chars long (bot ids
    already reach 10 digits) passed through untouched."""
    token = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDMARKR1"  # 36 chars
    url = f"https://api.telegram.org/bot12345678:{token}/sendMessage"

    assert token not in _redact_known_secret_shapes(url)


def test_a_telegram_token_as_the_final_url_segment_is_redacted() -> None:
    """The original regex required a trailing `/`, so a token with no method segment after it —
    or an 11-digit bot id — passed through untouched."""
    token = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDMARKR1"
    url = f"https://api.telegram.org/bot12345678901:{token}"  # 11-digit id, no trailing "/"

    assert token not in _redact_known_secret_shapes(url)


def test_an_anthropic_api_key_is_redacted() -> None:
    """M6.8 does not exist yet — nothing calls get_secret_value() on anthropic_api_key today —
    but the pattern is in place before that call site is, not discovered as a gap after it."""
    key = "sk-ant-api03-MARKERabcdefghijklmnopqrstuvwxyz1234567890"

    redacted = _redact_known_secret_shapes(f"request failed: key={key}")

    assert key not in redacted
    assert "sk-ant-***REDACTED***" in redacted


def test_an_aws_access_key_id_is_redacted() -> None:
    key = "AKIAABCDEFGHIJKLMNOP"

    redacted = _redact_known_secret_shapes(f"using access key {key}")

    assert key not in redacted


@pytest.mark.parametrize("field", ["alert_smtp_password", "kite_api_key", "kite_api_secret"])
def test_a_shapeless_credential_is_redacted_by_field_name(field: str) -> None:
    """SMTP and Kite credentials have no fixed shape at all — nothing about the VALUE can be
    pattern-matched, unlike Anthropic's `sk-ant-` prefix or a Telegram token's `\\d+:` shape — so
    the only signal available is the name a value is logged under."""
    marker = "anyRandomShapeAtAll-9f2c"

    redacted = _redact_known_secret_shapes({field: marker})

    assert marker not in str(redacted)
    assert redacted[field] == "***REDACTED***"


def test_a_secret_inside_a_tuple_or_a_set_is_redacted() -> None:
    """The recursive descent originally covered dict and list only; a caller collecting several
    DSNs into a tuple or a set fell through untouched."""
    dsn = "postgresql://trading:s3cret-tuple-9f2c@localhost:5433/trading"

    assert "s3cret-tuple-9f2c" not in str(_redact_known_secret_shapes((dsn,)))
    assert "s3cret-tuple-9f2c" not in str(_redact_known_secret_shapes({dsn}))


def test_a_dataclass_carrying_a_bare_secret_field_is_redacted_via_its_repr() -> None:
    """`SecretStr` masks its own repr already; a *bare* str field on a plain config-carrying
    object does not, and JSONRenderer would otherwise repr() it after this processor has run."""

    @dataclass
    class SmtpCfgRepr:
        host: str
        password: str

    redacted = _redact_known_secret_shapes(SmtpCfgRepr(host="mail", password="MARKER-smtp-pw"))

    assert "MARKER-smtp-pw" not in redacted
    assert isinstance(redacted, str)


def test_a_pydantic_model_carrying_a_bare_secret_field_is_redacted_via_its_repr() -> None:
    class SmtpCfgModel(BaseModel):
        host: str
        password: str

    redacted = _redact_known_secret_shapes(SmtpCfgModel(host="mail", password="MARKER-smtp-pw"))

    assert "MARKER-smtp-pw" not in redacted


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1234.56"),
        date(2026, 8, 10),
        UUID("12345678-1234-5678-1234-567812345678"),
    ],
)
def test_ordinary_value_types_pass_through_unchanged(value: object) -> None:
    """The dataclass/BaseModel repr fix must not widen into "repr() anything unrecognised" — a
    Decimal, a date or a UUID is exactly the value shape this platform's own logging convention
    relies on structlog/JSONRenderer to serialise cleanly, and none of them can carry a credential
    field the way a config-carrying object can."""
    assert _redact_known_secret_shapes(value) is value
