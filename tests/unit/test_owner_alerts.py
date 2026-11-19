"""OPS-ALERTS: the owner's plain-English alerts.

Every builder is pure and proved directly; the onset/repeat/clear behaviour is proved against an
in-memory ledger (the same `reconcile` / `send_once` the scheduler runs), so "once per onset, once
on clear" does not depend on a database. No test touches the network or the lake.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.alert_triggers import (
    AlertCondition,
    ConditionLedger,
    TickReport,
    Trigger,
    reconcile,
    send_once,
)
from dataplatform.alerts import TELEGRAM_MAX_CHARS, BaseAlerter, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.owner_alerts import (
    BENCH,
    MAX_MESSAGE_CHARS,
    BookMarks,
    BookNight,
    BookReturn,
    DatasetFreshness,
    JobsDay,
    M17Night,
    Message,
    OpenAlert,
    PaperActivity,
    PaperFigures,
    PaperOrder,
    SummaryInputs,
    SwitchState,
    book_name,
    build_morning_summary,
    degraded_message,
    drawdown_conditions,
    drawdown_resolution,
    fit,
    inr,
    kill_switch_conditions,
    kill_switch_resolution,
    m17_night_message,
    paper_activity_message,
    pct,
    pending_decisions_message,
    session_drop_message,
    sessions_behind,
    stale_data_conditions,
    stale_resolution,
)
from dataplatform.scheduler.registry import default_registry
from tests.conftest import SettingsLoader

NOW = datetime(2026, 11, 19, 8, 0, tzinfo=IST)
D13 = "momentum_v2_paper_2026_09_06"
SECOND = "momentum_v2_turn_srefill_paper_2026_11_09"
#: Fake credentials, assembled at run time so the secret scan never sees one in the source.
_FAKE = {"dsn_pw": "hunter" + "2", "pw": "s3c" + "r3t"}
SECRETS = (
    "postgresql://trader:" + _FAKE["dsn_pw"] + "@db:5432/trading",
    "https://api.telegram.org/bot123456:AAH-secret_tok/sendMessage",
    "pass" + "word=" + _FAKE["pw"],
)


class RecordingAlerter(BaseAlerter):
    channel = "test"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(clock=FrozenClock(NOW), dedup_window=timedelta(0))
        self.sent: list[tuple[Severity, str, str, str]] = []
        self.fail = fail

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        if self.fail:
            raise ConnectionError("relay down")
        self.sent.append((severity, title, body, dedup_key))


class MemoryLedger(ConditionLedger):
    """`ConditionLedger` over a dict: key -> (trigger, title, resolved)."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[Trigger, str, bool]] = {}
        self.severity: dict[str, str] = {}

    @contextmanager
    def transaction(self) -> Iterator[object]:
        snapshot, severity = dict(self.rows), dict(self.severity)
        try:
            yield self
        except BaseException:
            self.rows, self.severity = snapshot, severity
            raise

    def open_keys(self, trigger: Trigger) -> dict[str, str]:
        return {k: t for k, (tr, t, done) in self.rows.items() if tr is trigger and not done}

    def open_severities(self, trigger: Trigger) -> dict[str, str]:
        return {k: self.severity[k] for k in self.open_keys(trigger)}

    def open(self, condition: AlertCondition, at: datetime) -> None:
        self.rows[condition.dedup_key] = (condition.trigger, condition.title, False)
        self.severity[condition.dedup_key] = condition.severity.value

    def set_severity(self, key: str, severity: Severity, at: datetime) -> None:
        self.severity[key] = severity.value

    def touch(self, keys: Sequence[str], at: datetime) -> None:
        pass

    def retitle(self, key: str, title: str) -> None:
        trigger, _, done = self.rows[key]
        self.rows[key] = (trigger, title, done)

    def resolve(self, key: str, at: datetime) -> None:
        trigger, title, _ = self.rows[key]
        self.rows[key] = (trigger, title, True)

    def has_key(self, key: str) -> bool:
        return key in self.rows

    def record_event(
        self, trigger: Trigger, key: str, severity: Severity, title: str, at: datetime
    ) -> None:
        self.rows[key] = (trigger, title, True)


def _reconcile(
    trigger: Trigger,
    conditions: list[AlertCondition],
    ledger: MemoryLedger,
    alerter: RecordingAlerter,
    resolution: object = None,
) -> TickReport:
    report = TickReport()
    reconcile(
        trigger,
        conditions,
        ledger=ledger,
        alerter=alerter,
        at=NOW,
        report=report,
        resolution=resolution,  # type: ignore[arg-type]
    )
    return report


def _all_text(message: Message | AlertCondition) -> str:
    return f"{message.title}\n{message.body}"


# ── wording ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (Decimal(985_661), "₹9.86 L"),
        (Decimal(10_000_000), "₹1.00 Cr"),
        (Decimal(102_345_678), "₹10.23 Cr"),
        (Decimal(70_441), "₹70,441"),
        (Decimal(-250_000), "-₹2.50 L"),
        (Decimal(999), "₹999"),
    ],
)
def test_rupees_read_in_lakh_and_crore(amount: Decimal, text: str) -> None:
    assert inr(amount) == text


def test_percentages_are_signed_with_one_decimal() -> None:
    assert pct(Decimal("1.234")) == "+1.2%"
    assert pct(Decimal("-0.86")) == "-0.9%"
    assert pct(Decimal("-0.04")) == "0.0%"
    assert pct(None) == "n/a"


def test_books_have_the_owners_names_not_ids() -> None:
    assert book_name(D13) == "Current momentum book (D13)"
    assert book_name(SECOND) == "Second momentum book (early entry)"
    assert book_name("FM-SWING-BRK-10L") == "Breakout swing ₹10 L"
    assert book_name("CTRL-FM-POS-FUND-1CR") == "Fundamentals ₹1 Cr (no-AI control)"
    assert book_name("m17_fund_managers") == "AI fund managers (all their books)"


def test_fit_cuts_long_text_under_the_telegram_limit() -> None:
    text = "\n".join("line " + "x" * 90 for _ in range(200))
    cut = fit(text)
    assert len(cut) <= MAX_MESSAGE_CHARS < TELEGRAM_MAX_CHARS
    assert cut.endswith("(shortened)")


def test_a_message_body_never_carries_a_credential() -> None:
    message = Message(Severity.INFO, f"t {SECRETS[2]}", "\n".join(SECRETS))
    for secret in ("hunter2", "AAH-secret", "s3cr3t"):
        assert secret not in _all_text(message)


# ── paper books ──────────────────────────────────────────────────────────────────────────────


def _activity(**fields: object) -> PaperActivity:
    base: dict[str, object] = {
        "book_id": SECOND,
        "session": date(2026, 11, 11),
        "outcome": "COMPLETED",
        "reason": "decided",
        "rebalanced": False,
        "orders": (),
        "next_session": date(2026, 11, 12),
        "was_in_cash": False,
    }
    base.update(fields)
    return PaperActivity(**base)  # type: ignore[arg-type]


def test_a_quiet_paper_session_sends_nothing() -> None:
    assert paper_activity_message(_activity()) is None


def test_buys_name_the_count_the_money_and_the_biggest_names() -> None:
    orders = tuple(PaperOrder("BUY", f"NAME{i}", Decimal(50_000 + i * 1_000)) for i in range(20))
    message = paper_activity_message(
        _activity(reason="early_reentry", orders=orders, was_in_cash=True)
    )
    assert message is not None
    assert "20 buys" in message.title and "moving into the market" in message.title
    assert "Thu 12 Nov's open" in message.body
    assert "₹11.90 L" in message.body  # 20 x 50,000 + 1,000 x (0 + … + 19)
    assert "NAME19 ₹69,000" in message.body
    assert "re-entered the market early" in message.body
    assert "Nothing for you to do" in message.body


def test_a_risk_off_rebalance_with_no_orders_says_it_stays_in_cash() -> None:
    message = paper_activity_message(
        _activity(book_id=D13, reason="rebalance_risk_off", rebalanced=True)
    )
    assert message is not None
    assert "stays in cash" in message.title
    assert message.body.count("market filter") == 1


def test_sells_on_a_risk_off_rebalance_are_moving_out_of_the_market() -> None:
    orders = (PaperOrder("SELL", "ABC", Decimal(100_000)), PaperOrder("SELL", "DEF", None))
    message = paper_activity_message(
        _activity(reason="rebalance_risk_off", rebalanced=True, orders=orders)
    )
    assert message is not None
    assert "moving out of the market" in message.title
    assert "at least ₹1.00 L to raise" in message.body


def test_a_red_session_is_a_skip_warning_with_the_reason_redacted() -> None:
    message = paper_activity_message(
        _activity(outcome="SKIPPED_DATA_RED", reason=f"status check failed {SECRETS[0]}")
    )
    assert message is not None and message.severity is Severity.WARNING
    assert "skipped" in message.title and "hunter2" not in message.body


def test_a_reconciliation_break_needs_the_owner() -> None:
    message = paper_activity_message(_activity(outcome="RECON_BREAK"))
    assert message is not None and message.severity is Severity.CRITICAL
    assert "Needs you" in message.body


# ── the M17 night and the degraded alert ─────────────────────────────────────────────────────


def _night(**fields: object) -> M17Night:
    base: dict[str, object] = {
        "session": date(2026, 11, 18),
        "outcome": "COMPLETED",
        "recorded_at": datetime(2026, 11, 18, 22, 22, tzinfo=IST),
        "books": {
            "FM-SWING-BRK-10L": BookNight(buys=2),
            "FM-SWING-BRK-1CR": BookNight(buys=2, refused=1),
            "FM-POS-FUND-10L": BookNight(buys=1),
            "FM-POS-FUND-1CR": BookNight(buys=1),
        },
    }
    base.update(fields)
    return M17Night(**base)  # type: ignore[arg-type]


RETURNS = [
    BookReturn("FM-SWING-BRK-10L", Decimal(1_010_000), Decimal(1), Decimal("0.5")),
    BookReturn(BENCH, Decimal(991_947), Decimal("-0.8"), None),
]


def test_the_nightly_summary_lists_each_manager_and_ends_all_normal() -> None:
    message = m17_night_message(_night(), RETURNS, date(2026, 11, 18))
    assert message.severity is Severity.INFO
    assert "finished at 22:22 IST" in message.body
    assert "Breakout swing: 2 buys, 0 sells (₹1 Cr copy 2), 1 refused" in message.body
    assert "+1.0% vs control +0.5%" in message.body
    assert "Market (NIFTY 500) since Wed 18 Nov: -0.8%" in message.body
    assert message.body.endswith("All normal. Nothing for you to do.")


def test_a_manager_error_makes_the_nightly_summary_ask_for_the_owner() -> None:
    books = dict(_night().books)
    books["FM-POS-FUND-10L"] = BookNight(errors=1)
    message = m17_night_message(_night(books=books), RETURNS, None)
    assert message.severity is Severity.WARNING and "Needs you" in message.body


def test_the_nightly_summary_never_mentions_cost() -> None:
    text = _all_text(m17_night_message(_night(fallback_calls={"bedrock": 3}), RETURNS, None))
    assert not any(word in text.lower() for word in ("cost", "$", "usd", "token", "spend"))


def test_a_clean_night_is_not_degraded() -> None:
    assert degraded_message(date(2026, 11, 18), _night(), []) is None


def test_a_degraded_night_names_what_fell_back_and_how_many_calls() -> None:
    night = _night(
        fallback_calls={"bedrock": 3, "bedrock_mantle": 1},
        digest_fallbacks=5,
        data_gaps="NIFTY 500 TRI for 2026-11-18",
    )
    skipped = [_activity(book_id=D13, outcome="SKIPPED_DATA_RED")]
    message = degraded_message(date(2026, 11, 18), night, skipped)
    assert message is not None and message.severity is Severity.WARNING
    assert "4 decision call(s)" in message.body
    assert "5 were written by the backup model" in message.body
    assert "NIFTY 500 TRI" in message.body
    assert "Current momentum book (D13) skipped" in message.body
    assert "cost" not in message.body.lower()


# ── loss limits ──────────────────────────────────────────────────────────────────────────────


def _marks(*navs: int, book_id: str = SECOND, opening: int = 1_000_000) -> BookMarks:
    start = date(2026, 11, 2)
    return BookMarks(
        book_id,
        Decimal(opening),
        tuple((start + timedelta(days=i), Decimal(n)) for i, n in enumerate(navs)),
    )


def _drawdown(book: BookMarks) -> list[AlertCondition]:
    return drawdown_conditions([book], threshold=Decimal(10), step=Decimal(5))


def test_a_drawdown_at_or_below_the_limit_raises_nothing() -> None:
    assert _drawdown(_marks(1_100_000, 990_000)) == []  # exactly 10% below the peak


def test_a_drawdown_past_ten_percent_raises_one_condition() -> None:
    (condition,) = _drawdown(_marks(1_100_000, 980_000))
    assert condition.dedup_key == f"owner:loss:{SECOND}:drawdown:10"
    assert "10.9% below its peak of ₹11.00 L" in condition.body
    assert "Needs you" in condition.body


def test_each_further_five_points_is_a_new_condition() -> None:
    keys = [c.dedup_key for c in _drawdown(_marks(1_000_000, 830_000))]
    assert keys == [f"owner:loss:{SECOND}:drawdown:10", f"owner:loss:{SECOND}:drawdown:15"]


def test_drawdown_pages_once_per_level_and_once_when_back_within_the_limit() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter()
    resolution = drawdown_resolution({}, threshold=Decimal(10))

    def run(book: BookMarks) -> None:
        _reconcile(Trigger.OWNER_LOSS, _drawdown(book), ledger, alerter, resolution)

    run(_marks(1_000_000, 880_000))  # 12%: onset
    run(_marks(1_000_000, 870_000))  # 13%: same level, silent
    run(_marks(1_000_000, 840_000))  # 16%: the 15% level pages
    run(_marks(1_000_000, 870_000))  # 13%: the 15% level clears quietly
    run(_marks(1_000_000, 950_000))  # 5%: back within limits, one message
    run(_marks(1_000_000, 960_000))  # still fine: nothing
    titles = [sent[1] for sent in alerter.sent]
    assert len(titles) == 3
    assert "down more than 10%" in titles[0]
    assert "down more than 15%" in titles[1] and alerter.sent[1][0] is Severity.CRITICAL
    assert "back within its loss limit" in titles[2]


def test_a_one_session_drop_past_the_limit_is_news() -> None:
    message = session_drop_message(_marks(1_000_000, 955_000), threshold=Decimal(4))
    assert message is not None and "fell 4.5%" in message.title


def test_a_one_session_drop_within_the_limit_is_not() -> None:
    assert session_drop_message(_marks(1_000_000, 961_000), threshold=Decimal(4)) is None
    assert session_drop_message(_marks(1_000_000, 1_100_000), threshold=Decimal(4)) is None


def test_the_first_session_drops_from_the_opening_capital() -> None:
    message = session_drop_message(_marks(950_000), threshold=Decimal(4))
    assert message is not None and "₹10.00 L to ₹9.50 L" in message.body


# ── kill switches ────────────────────────────────────────────────────────────────────────────


def test_an_armed_switch_raises_nothing() -> None:
    assert kill_switch_conditions([SwitchState("m17_fund_managers", tripped=False)]) == []


def test_a_tripped_switch_is_critical_and_says_who_and_why() -> None:
    (condition,) = kill_switch_conditions(
        [
            SwitchState(
                D13,
                tripped=True,
                source="MANUAL",
                reason=f"testing {SECRETS[0]}",
                tripped_at=datetime(2026, 11, 19, 14, 2, tzinfo=IST),
            )
        ]
    )
    assert condition.severity is Severity.CRITICAL
    assert condition.title == "Trading halted: Current momentum book (D13)"
    assert "tripped by hand at 14:02 IST on Thu 19 Nov" in condition.body
    assert "hunter2" not in condition.body and "Needs you" in condition.body


def test_an_unreadable_switch_counts_as_tripped() -> None:
    (condition,) = kill_switch_conditions([SwitchState("m17_fund_managers", tripped=None)])
    assert "cannot be read" in condition.body


def test_trip_pages_once_and_reset_pages_once_critical() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter()
    resolution = kill_switch_resolution(lambda account: "cause fixed")
    tripped = [SwitchState("m17_fund_managers", tripped=True, source="RECON")]
    for _ in range(3):
        _reconcile(
            Trigger.OWNER_KILL_SWITCH, kill_switch_conditions(tripped), ledger, alerter, resolution
        )
    armed = [SwitchState("m17_fund_managers", tripped=False)]
    for _ in range(2):
        _reconcile(
            Trigger.OWNER_KILL_SWITCH, kill_switch_conditions(armed), ledger, alerter, resolution
        )
    assert [(s[0], s[1]) for s in alerter.sent] == [
        (Severity.CRITICAL, "Trading halted: AI fund managers (all their books)"),
        (Severity.CRITICAL, "Trading resumed: AI fund managers (all their books)"),
    ]
    assert "Note: cause fixed" in alerter.sent[1][2]


# ── stale data ───────────────────────────────────────────────────────────────────────────────


def test_sessions_behind_counts_sessions_after_the_latest_and_before_today() -> None:
    def weekdays(start: date, end: date) -> list[date]:
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [d for d in days if d.weekday() < 5]

    thursday = date(2026, 11, 19)
    assert sessions_behind(date(2026, 11, 18), thursday, weekdays) == 0
    assert sessions_behind(date(2026, 11, 13), thursday, weekdays) == 3  # Mon, Tue, Wed
    assert sessions_behind(date(2026, 11, 13), date(2026, 11, 16), weekdays) == 0  # Monday


def _fresh(
    key: str, behind: int | None, latest: date | None = date(2026, 11, 13)
) -> DatasetFreshness:
    return DatasetFreshness(key, "India VIX", latest, behind)


def test_two_sessions_behind_is_not_stale_three_is() -> None:
    assert stale_data_conditions([_fresh("india_vix", 2)], max_sessions=2) == []
    (condition,) = stale_data_conditions([_fresh("india_vix", 3)], max_sessions=2)
    assert condition.severity is Severity.WARNING
    assert "from Fri 13 Nov, 3 trading sessions behind" in condition.body


def test_a_dataset_with_no_data_at_all_is_stale() -> None:
    (condition,) = stale_data_conditions([_fresh("india_vix", None, None)], max_sessions=2)
    assert "no India VIX data at all" in condition.body


def test_the_known_midcap_gap_alerts_once_and_says_it_is_known() -> None:
    known = DatasetFreshness(
        "tri_midsmall", "Midcap TRI", date(2026, 10, 1), 33, "This is a known gap."
    )
    ledger, alerter = MemoryLedger(), RecordingAlerter()
    for _ in range(4):
        _reconcile(
            Trigger.OWNER_STALE_DATA,
            stale_data_conditions([known], max_sessions=2),
            ledger,
            alerter,
        )
    assert len(alerter.sent) == 1 and "known gap" in alerter.sent[0][2]


def test_stale_data_clears_once_when_it_catches_up() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter()
    late, caught_up = _fresh("india_vix", 4), _fresh("india_vix", 0, date(2026, 11, 18))
    resolution = stale_resolution({"india_vix": caught_up})
    _reconcile(
        Trigger.OWNER_STALE_DATA, stale_data_conditions([late], max_sessions=2), ledger, alerter
    )
    for _ in range(2):
        _reconcile(
            Trigger.OWNER_STALE_DATA,
            stale_data_conditions([caught_up], max_sessions=2),
            ledger,
            alerter,
            resolution,
        )
    assert [s[1] for s in alerter.sent] == ["Data is late: India VIX", "Data caught up: India VIX"]
    assert "newest: Wed 18 Nov" in alerter.sent[1][2]


# ── send once ────────────────────────────────────────────────────────────────────────────────


def _send(ledger: MemoryLedger, alerter: RecordingAlerter, key: str) -> TickReport:
    report = TickReport()
    send_once(
        ledger=ledger,
        alerter=alerter,
        trigger=Trigger.OWNER_EVENT,
        key=key,
        severity=Severity.INFO,
        title="t",
        body="b",
        at=NOW,
        report=report,
    )
    return report


def test_a_one_shot_message_is_sent_once_per_key() -> None:
    ledger, alerter = MemoryLedger(), RecordingAlerter()
    for _ in range(3):
        _send(ledger, alerter, "owner:m17-night:2026-11-18")
    _send(ledger, alerter, "owner:m17-night:2026-11-19")
    assert [s[3] for s in alerter.sent] == [
        "owner:m17-night:2026-11-18",
        "owner:m17-night:2026-11-19",
    ]


def test_a_one_shot_message_that_failed_is_tried_again() -> None:
    ledger = MemoryLedger()
    report = _send(ledger, RecordingAlerter(fail=True), "owner:summary:2026-11-19")
    assert not report.ok and not ledger.has_key("owner:summary:2026-11-19")
    alerter = RecordingAlerter()
    assert _send(ledger, alerter, "owner:summary:2026-11-19").ok
    assert len(alerter.sent) == 1


# ── the morning summary ──────────────────────────────────────────────────────────────────────


def _inputs(**fields: object) -> SummaryInputs:
    base: dict[str, object] = {
        "today": date(2026, 11, 19),
        "jobs": JobsDay(date(2026, 11, 18), ok=19, failed=()),
        "night": _night(),
        "m17_returns": RETURNS,
        "s0": date(2026, 11, 18),
        "papers": [
            PaperFigures(
                SECOND,
                date(2026, 11, 18),
                Decimal("985661.61"),
                Decimal("98.6"),
                Decimal("-0.9"),
                Decimal("-1.4"),
                Decimal("0.7"),
                date(2026, 11, 9),
            )
        ],
        "open_alerts": [
            OpenAlert("old", "critical", datetime(2026, 10, 7, 8, 0, tzinfo=IST)),
        ],
        "disk_pct": 77,
    }
    base.update(fields)
    return SummaryInputs(**base)  # type: ignore[arg-type]


def test_a_normal_morning_says_all_normal() -> None:
    message = build_morning_summary(_inputs(), disk_warn_pct=90)
    assert message.title == "Morning summary — Thu 19 Nov"
    body = message.body
    assert "Scheduled jobs on Wed 18 Nov: all 19 fine." in body
    assert "night of Wed 18 Nov: completed 22:22 IST" in body
    assert "Second momentum book (early entry): ₹9.86 L, 98.6% in stocks; day -0.9%" in body
    assert "vs NIFTY 50 TRI +0.7%" in body
    assert "Open alerts: 1 (oldest since Wed 7 Oct); none new." in body
    assert "Disk: 77% used." in body
    assert body.endswith("All normal.")
    assert message.severity is Severity.INFO


def test_a_failed_job_and_a_full_disk_need_the_owner() -> None:
    message = build_morning_summary(
        _inputs(jobs=JobsDay(date(2026, 11, 18), ok=17, failed=("tri_evening",)), disk_pct=93),
        disk_warn_pct=90,
    )
    assert "failed: tri evening" in message.body
    assert message.body.splitlines()[-1].startswith("Needs you: a scheduled job failed")
    assert "the disk is 93% full" in message.body
    assert message.severity is Severity.WARNING


def test_a_tripped_switch_and_an_unreadable_part_need_the_owner() -> None:
    message = build_morning_summary(
        _inputs(papers=None, tripped=["m17_fund_managers"], unreadable=["the momentum books"]),
        disk_warn_pct=90,
    )
    last = message.body.splitlines()[-1]
    assert "trading is halted for AI fund managers" in last
    assert last.count("the momentum books could not be read") == 1


def test_the_weekend_summary_is_shorter() -> None:
    weekday = build_morning_summary(_inputs(), disk_warn_pct=90)
    sunday = build_morning_summary(_inputs(today=date(2026, 11, 22)), disk_warn_pct=90)
    assert len(sunday.body) < len(weekday.body)
    assert "vs their no-AI control" not in sunday.body and "day -0.9%" not in sunday.body


def test_the_summary_fits_telegram_whatever_the_inputs() -> None:
    alerts = [OpenAlert("x" * 300, "critical", NOW - timedelta(hours=1)) for _ in range(500)]
    message = build_morning_summary(_inputs(open_alerts=alerts), disk_warn_pct=90)
    assert len(f"[WARNING] {message.title}\n\n{message.body}") < 4000


def test_the_summary_title_can_be_the_sample_title() -> None:
    message = build_morning_summary(_inputs(), disk_warn_pct=90, title="Sample morning summary")
    assert message.title == "Sample morning summary"


# ── the Monday list ──────────────────────────────────────────────────────────────────────────


def test_monday_lists_only_the_open_decisions(tmp_path: Path) -> None:
    path = tmp_path / "pending-decisions.md"
    path.write_text(
        "# header\n- [done 2026-11-16] decided thing\n- [open] Thin-traded label — whether\n"
        "- [open] M17 A5 — confirm\n",
        encoding="utf-8",
    )
    message = pending_decisions_message(path, date(2026, 11, 23))
    assert message is not None and message.title == "Decisions waiting on you"
    assert "1. Thin-traded label" in message.body and "2. M17 A5" in message.body
    assert "decided thing" not in message.body


def test_monday_with_nothing_open_sends_nothing(tmp_path: Path) -> None:
    path = tmp_path / "pending-decisions.md"
    path.write_text("- [done] all decided\n", encoding="utf-8")
    assert pending_decisions_message(path, date(2026, 11, 23)) is None


def test_a_missing_decisions_file_is_said(tmp_path: Path) -> None:
    message = pending_decisions_message(tmp_path / "gone.md", date(2026, 11, 23))
    assert message is not None and "could not be read" in message.body


# ── wiring ───────────────────────────────────────────────────────────────────────────────────


def test_the_owner_jobs_are_registered_on_their_schedules() -> None:
    registry = default_registry()
    assert registry.get("owner_alerts").cron == "*/15 * * * *"
    assert registry.get("owner_morning_summary").cron == "0 8 * * *"
    assert registry.get("owner_weekly_decisions").cron == "0 9 * * mon"


def test_the_thresholds_are_settings_with_the_owners_defaults(
    load_settings: SettingsLoader,
) -> None:
    settings = load_settings(None)
    assert settings.owner_alert_drawdown_pct == Decimal(10)
    assert settings.owner_alert_drawdown_step_pp == Decimal(5)
    assert settings.owner_alert_session_drop_pct == Decimal(4)
    assert settings.owner_alert_stale_sessions == 2
    assert settings.owner_pending_decisions_file.is_absolute()
