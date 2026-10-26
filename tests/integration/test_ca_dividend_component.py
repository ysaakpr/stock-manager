"""Migration 0020 and the component key against Postgres: backfill, two dividends a day, re-parse.

The store is first migrated to 0019 and given dividend rows the way the one-dividend key held them;
then 0020 is applied over them. Runs against a scratch database created for the session and dropped
afterwards, never the developer's `trading` database; each test rolls back.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.corpactions.taxonomy import ActionType, DividendKind, DividendTerms
from dataplatform.identity.master import Exchange, IdentityStore, ListingStatus, Security
from dataplatform.ingest.ca_dividend_reparse import DayGroup, ReparsePlan, apply_reparse
from dataplatform.ingest.corp_actions import (
    CorporateAction,
    load_corporate_actions,
    write_corporate_actions,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import MIGRATIONS_DIR, migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_ca_component_{os.getpid()}"
CLOCK = FrozenClock(datetime(2026, 10, 24, 10, 0, tzinfo=IST))
LINDE: Final = "INE473A01011"
EX: Final = date(2025, 8, 7)
NSE_LINE: Final = "Dividend - Rs 4.50 Per Share & Special Dividend - Rs 7.50 Per Share"


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
def scratch_settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """A scratch database migrated to 0019, seeded with one-dividend-key rows, then to head."""
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    settings = _settings_for(SCRATCH_DB)
    before = tmp_path_factory.mktemp("migrations_0019")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql.name < "0020":
            shutil.copy(sql, before / sql.name)
    migrate(settings, clock=CLOCK, directory=before)
    with connection(settings) as seed:
        IdentityStore(seed, clock=CLOCK).write_securities(
            [Security(LINDE, "Linde India", Exchange.NSE, ListingStatus.ACTIVE, date(2000, 1, 1))]
        )
        for source, terms, raw in (
            ("bse_corp_actions", {"dividend_kind": "FINAL", "amount_inr": "4.5"}, "Final Dividend"),
            ("nse_corp_actions", {"dividend_kind": "SPECIAL", "amount_inr": None}, NSE_LINE),
        ):
            seed.execute(
                "INSERT INTO corporate_actions (isin, ex_date, action_type, ratio_terms, "
                "knowable_date, source, raw_text, recorded_at) VALUES (%s, %s, 'DIVIDEND', %s, "
                "%s, %s, %s, %s)",
                (
                    LINDE,
                    EX,
                    json.dumps({"kind": "dividend", "percent_of_face_value": None, **terms}),
                    date(2025, 7, 24),
                    source,
                    raw,
                    CLOCK.now(),
                ),
            )
        seed.commit()
    migrate(settings, clock=CLOCK)
    yield settings

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


def _div(source: str, kind: DividendKind, amount: str | None, raw: str) -> CorporateAction:
    return CorporateAction(
        isin=LINDE,
        ex_date=EX,
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(
            dividend_kind=kind, amount_inr=None if amount is None else Decimal(amount)
        ),
        source=source,
        raw_text=raw,
        knowable_date=date(2026, 10, 24),
    )


def test_0020_backfills_each_stored_dividend_with_its_kind(conn: Connection) -> None:
    rows = conn.execute(
        "SELECT source, component FROM corporate_actions WHERE isin = %s ORDER BY source", (LINDE,)
    ).fetchall()
    assert rows == [("bse_corp_actions", "FINAL"), ("nse_corp_actions", "SPECIAL")]
    stored = load_corporate_actions(conn, isin=LINDE)
    assert [a.component for a in stored] == ["FINAL", "SPECIAL"]  # and they load: the kinds agree


def test_a_feed_may_now_hold_two_dividends_on_one_day_but_not_one_twice(conn: Connection) -> None:
    special = _div("bse_corp_actions", DividendKind.SPECIAL, "7.5", "Special Dividend - Rs. - 7.5")
    counts = write_corporate_actions(conn, [special], clock=CLOCK)
    assert counts.inserted == 1  # the old key refused this: BSE already had a dividend that day
    again = write_corporate_actions(conn, [special], clock=CLOCK)
    assert (again.inserted, again.skipped) == (0, 1)
    revised = _div("bse_corp_actions", DividendKind.FINAL, "5", "Final Dividend - Rs. - 5.0000")
    assert write_corporate_actions(conn, [revised], clock=CLOCK).inserted == 0  # same component


def test_the_reparse_replaces_the_stale_bare_row_and_adds_the_missing_one(conn: Connection) -> None:
    stored = tuple(
        a for a in load_corporate_actions(conn, isin=LINDE) if a.source.startswith("nse")
    )
    reparsed = (
        _div("nse_corp_actions", DividendKind.UNSPECIFIED, "4.5", NSE_LINE),
        _div("nse_corp_actions", DividendKind.SPECIAL, "7.5", NSE_LINE),
    )
    plan = ReparsePlan(
        groups=(DayGroup(key=(LINDE, EX, "nse_corp_actions"), stored=stored, reparsed=reparsed),)
    )
    counts = apply_reparse(conn, plan, clock=CLOCK)
    assert (counts.deleted, counts.inserted) == (1, 2)
    nse = [a for a in load_corporate_actions(conn, isin=LINDE) if a.source.startswith("nse")]
    assert sorted((a.component, a.terms.model_dump_json()) for a in nse) == sorted(
        (a.component, a.terms.model_dump_json()) for a in reparsed
    )
    # New rows were knowable when their stored sibling was, not on the day of the re-parse.
    assert {a.knowable_date for a in nse} == {date(2025, 7, 24)}
    assert Path(MIGRATIONS_DIR / "0020_ca_dividend_component.sql").is_file()


def test_a_dividend_without_a_component_is_refused_at_the_table(conn: Connection) -> None:
    """An INSERT that predates 0020 cannot slip a '' dividend in beside its kind-keyed twin."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO corporate_actions (isin, ex_date, action_type, ratio_terms, "
            "knowable_date, source, raw_text, recorded_at) VALUES (%s, %s, 'DIVIDEND', %s, %s, "
            "'bse_corp_actions', 'Final Dividend', %s)",
            (
                LINDE,
                date(2024, 8, 1),
                json.dumps({"kind": "dividend", "dividend_kind": "FINAL", "amount_inr": "1"}),
                date(2024, 7, 1),
                CLOCK.now(),
            ),
        )
