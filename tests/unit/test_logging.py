"""Structured logging renders what the dashboard and the alerting rules read (§8.1).

The sync dashboard, the failure-streak alert and every post-mortem query fields, not prose, so an
event that loses its `source` or its `logical_date` is a defect even though the line still looks
fine to a human. These tests assert the shape of the output, including that the timestamp comes
from the injected clock (B10) rather than the host.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import pytest
import structlog

from dataplatform.clock import IST, FrozenClock
from dataplatform.logging import (
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
