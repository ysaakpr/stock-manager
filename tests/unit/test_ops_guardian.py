"""OPS-GUARD: the ops guardian's heartbeat file becomes an Ops row and pages through the ledger.

The guardian itself is a box-local shell service; what the repo owns is the bridge — parse its
file as untrusted text, fail when its heartbeat is stale, and page its alert onsets and
resolutions exactly once through the configured alerter. Offline: the ledger is the in-memory
one `test_alert_triggers` proves `reconcile` against.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dataplatform.alert_triggers import Trigger
from dataplatform.alerts import Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.ops_guardian import (
    KEY_PREFIX,
    GuardianStaleError,
    GuardianStateError,
    guardian_conditions,
    main,
    parse_state,
    read_state,
    run_ops_guardian,
)
from dataplatform.scheduler.registry import default_registry
from tests.conftest import SettingsLoader
from tests.unit.test_alert_triggers import MemoryLedger, RecordingAlerter

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=IST)
STALE_AFTER = timedelta(minutes=15)
MIGRATIONS = Path(__file__).resolve().parents[2] / "dataplatform" / "store" / "migrations"


def _state(*alerts: tuple[str, str], beat: datetime = NOW) -> str:
    return json.dumps(
        {
            "heartbeat_at": beat.isoformat(),
            "pid": 4242,
            "alerts": [
                {"key": key, "message": message, "since": NOW.isoformat()}
                for key, message in alerts
            ],
        }
    )


def _run(text: str, ledger: MemoryLedger, alerter: RecordingAlerter, now: datetime = NOW) -> None:
    run_ops_guardian(
        read=lambda: parse_state(text),
        ledger=ledger,
        alerter=alerter,
        now=now,
        stale_after=STALE_AFTER,
    )


# ── parsing: the file is untrusted ──────────────────────────────────────────────────────────


def test_a_well_formed_state_parses_and_ignores_the_guardians_own_fields() -> None:
    state = parse_state(_state(("job_eod_pipeline", "eod_pipeline OVERDUE")))
    assert state.heartbeat_at == NOW
    assert [(a.key, a.message) for a in state.alerts] == [
        ("job_eod_pipeline", "eod_pipeline OVERDUE")
    ]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"alerts": []}),
        json.dumps({"heartbeat_at": "2026-11-10T15:00:00", "alerts": []}),  # naive
        json.dumps({"heartbeat_at": NOW.isoformat(), "alerts": {}}),
        json.dumps(
            {"heartbeat_at": NOW.isoformat(), "alerts": [{"key": "Bad Key", "message": ""}]}
        ),
        json.dumps({"heartbeat_at": NOW.isoformat(), "alerts": [{"key": "ok"}]}),
    ],
)
def test_a_malformed_state_is_an_error_not_an_empty_alert_set(text: str) -> None:
    with pytest.raises(GuardianStateError):
        parse_state(text)


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GuardianStateError, match="unreadable"):
        read_state(tmp_path / "guardian.json")


def test_a_credential_in_a_guardian_message_never_reaches_the_page() -> None:
    token = "sk-ant-" + "a" * 40
    state = parse_state(_state(("health", f"health said Bearer {token} and failed")))
    (condition,) = guardian_conditions(state)
    assert token not in condition.title and token not in condition.body


# ── freshness: the half of the mutual watch the Ops page shows ──────────────────────────────


def test_a_fresh_heartbeat_passes_and_a_stale_one_fails_the_run() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter(FrozenClock(NOW))
    _run(_state(beat=NOW - STALE_AFTER), ledger, alerter)  # exactly at the limit: still alive
    with pytest.raises(GuardianStaleError, match="16 min old"):
        _run(_state(beat=NOW - STALE_AFTER - timedelta(minutes=1)), ledger, alerter)


def test_a_stale_heartbeat_resolves_nothing() -> None:
    """Inverted, this would page "resolved" for every guardian alert when the guardian died."""
    ledger, alerter = MemoryLedger(), RecordingAlerter(FrozenClock(NOW))
    _run(_state(("disk", "disk 93% full")), ledger, alerter)
    later = NOW + timedelta(hours=1)
    with pytest.raises(GuardianStaleError):
        _run(_state(beat=NOW), ledger, alerter, now=later)  # no alerts, but a dead guardian
    assert ledger.open_keys(Trigger.OPS_GUARDIAN) == {
        f"{KEY_PREFIX}disk": "ops guardian: disk 93% full"
    }
    assert len(alerter.sent) == 1


# ── paging: once per onset, once per resolution ─────────────────────────────────────────────


def test_a_guardian_alert_pages_once_on_onset_and_once_on_resolution() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter(FrozenClock(NOW))
    text = _state(("agenda_backfill-wed_overdue", "agenda backfill-wed past its deadline"))
    _run(text, ledger, alerter)
    _run(text, ledger, alerter, now=NOW + timedelta(minutes=5))  # a repeat is the same news
    assert [(s, k) for s, _, _, k in alerter.sent] == [
        (Severity.CRITICAL, "guardian:agenda_backfill-wed_overdue")
    ]
    _run(_state(), ledger, alerter, now=NOW + timedelta(minutes=10))
    assert [(s, k) for s, _, _, k in alerter.sent][1:] == [
        (Severity.INFO, "guardian:agenda_backfill-wed_overdue:resolved")
    ]
    assert ledger.open_keys(Trigger.OPS_GUARDIAN) == {}


def test_a_message_that_changes_wording_is_the_same_alert() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter(FrozenClock(NOW))
    _run(_state(("bt_stall", "log silent for 31 min")), ledger, alerter)
    _run(_state(("bt_stall", "log silent for 36 min")), ledger, alerter)
    assert len(alerter.sent) == 1


# ── wiring ──────────────────────────────────────────────────────────────────────────────────


def _effective_trigger_values() -> set[str]:
    values: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for match in re.finditer(r"CHECK \(trigger IN \((.*?)\)\)", path.read_text(), re.DOTALL):
            values = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    return values


def test_every_trigger_value_is_allowed_by_the_migrated_check() -> None:
    """A Trigger the CHECK does not allow is an onset that can never be written, so never pages."""
    assert {trigger.value for trigger in Trigger} == _effective_trigger_values()


def test_the_guardian_has_its_own_row_on_the_five_minute_cadence() -> None:
    job = default_registry().get("ops_guardian")
    assert job.cron == "*/5 * * * *"


def test_the_stale_limit_defaults_to_fifteen_minutes(load_settings: SettingsLoader) -> None:
    settings = load_settings(None)
    assert settings.ops_guardian_stale_after_minutes == 15
    assert settings.ops_guardian_state_file.is_absolute()


# ── notify: the direct path for when the scheduler is down ──────────────────────────────────


def test_notify_sends_through_the_log_channel(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALERT_PROVIDER", "log")
    code = main(
        ["notify", "--severity", "critical", "--key", "scheduler_stale", "--title", "down"],
        settings=load_settings(None),
        stdin=io.StringIO("scheduler heartbeat 20 min old"),
    )
    assert code == 0


def test_notify_refuses_an_unconfigured_channel_naming_only_the_keys(
    load_settings: SettingsLoader,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ALERT_PROVIDER", "telegram")
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "")
    code = main(
        ["notify", "--severity", "info", "--key", "x", "--title", "t"],
        settings=load_settings(None),
        stdin=io.StringIO(""),
    )
    assert code == 2
    assert "ALERT_TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
