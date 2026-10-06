"""PR-bundle L1: ISIN-keyed band hits, security marks, index EOD and CA broadcasts, from L0.

One real session, 2026-09-04, assembled from three frozen fixtures that each carry one member of
that day's bundle byte-for-byte (`lowercase/` → `bc`, `bh_no_flag/` → `bh`, `pd/lowercase/` →
`pd`). Identity is a synthetic `prices_raw` partition for the session plus a small in-memory
master, so every resolution path — and every quarantine reason — is exercised offline.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Final

import duckdb
import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.ingest.pr_bundle_l1 import (
    BAND_HITS_DATASET,
    CA_BROADCASTS_DATASET,
    INDEX_EOD_DATASET,
    PR_DATASETS,
    QUARANTINE_DATASET,
    SECURITY_MARKS_DATASET,
    QuarantineReason,
    ResolvedVia,
    index_id_for,
    rebuild_from_l0,
)
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"
SESSION: Final = date(2026, 9, 4)
ARCHIVE: Final = "PR040926.zip"

ADANIENT: Final = "INE423A01024"
AKI: Final = "INE642Z01018"
ARVIND: Final = "INE034A01011"
WRONG: Final = "INE000A01010"
AIAENG_1: Final = "INE212H01026"
AIAENG_2: Final = "INE212H01034"


def _members(*sources: tuple[str, str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for directory, member in sources:
        with zipfile.ZipFile(FIXTURES / directory / ARCHIVE) as z:
            out[member] = z.read(member)
    return out


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, body in members.items():
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 9, 4, 18, 0, 0)), body)
    return buffer.getvalue()


ALL_MEMBERS: Final = (
    ("lowercase", "bc04092026.csv"),
    ("bh_no_flag", "bh04092026.csv"),
    ("pd/lowercase", "pd04092026.csv"),
)


def _store(root: Path, members: tuple[tuple[str, str], ...] = ALL_MEMBERS) -> L0Store:
    store = L0Store(clock=FrozenClock(SESSION), data_root=root)
    store.put("nse_pr_bundle", SESSION, ARCHIVE, _zip(_members(*members)))
    return store


def _prices_raw(root: Path, rows: list[tuple[str, str, str]]) -> None:
    path = l1_partition_path("prices_raw", SESSION, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (isin VARCHAR, exchange VARCHAR, symbol VARCHAR, series VARCHAR, "
        "trade_date DATE)"
    )
    for symbol, series, isin in rows:
        con.execute("INSERT INTO t VALUES (?, 'NSE', ?, ?, ?)", [isin, symbol, series, SESSION])
    # A BSE row naming another ISIN for an NSE symbol: never an NSE statement.
    con.execute("INSERT INTO t VALUES (?, 'BSE', 'AKI', 'EQ', ?)", [WRONG, SESSION])
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    con.close()


def _master(*windows: tuple[str, str]) -> IdentityMaster:
    return IdentityMaster(
        SymbolWindow(
            exchange=Exchange.NSE, symbol=s, valid_from=date(2000, 1, 1), valid_to=None, isin=i
        )
        for s, i in windows
    )


def _read(root: Path, dataset: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = pq.read_table(
        l1_partition_path(dataset, SESSION, data_root=root)
    ).to_pylist()
    return rows


@pytest.fixture
def built(tmp_path: Path) -> Path:
    store = _store(tmp_path)
    _prices_raw(
        tmp_path,
        [
            ("ADANIENT", "EQ", ADANIENT),
            ("AKI", "EQ", AKI),
            ("AIAENG", "EQ", AIAENG_1),
            ("AIAENG", "EQ", AIAENG_2),  # stated twice: SessionIdentity refuses it
        ],
    )
    master = _master(("ADANIENT", WRONG), ("ARVIND", ARVIND), ("1003SCL32", WRONG))
    report = rebuild_from_l0(
        start=SESSION, end=SESSION, l0=store, master=master, data_root=tmp_path
    )
    assert (report.bundles, report.built, report.failures) == (1, 1, [])
    return tmp_path


def test_no_row_is_dropped(built: Path) -> None:
    quarantine = _read(built, QUARANTINE_DATASET)
    for member, dataset, published in (
        ("bh", BAND_HITS_DATASET, 286),
        ("pd", SECURITY_MARKS_DATASET, 3655),
        ("bc", CA_BROADCASTS_DATASET, 350),
    ):
        written = len(_read(built, dataset))
        held = sum(1 for row in quarantine if row["member"] == member)
        assert written + held == published, member
    assert len(_read(built, INDEX_EOD_DATASET)) == 139
    assert {row["reason"] for row in quarantine} <= {r.value for r in QuarantineReason}


def test_the_sessions_own_statement_wins_over_the_master(built: Path) -> None:
    """ADANIENT: the session says ADANIENT, the master says WRONG. Inverting the order fails."""
    marks = {r["symbol"]: r for r in _read(built, SECURITY_MARKS_DATASET) if r["series"] == "EQ"}
    assert marks["ADANIENT"]["isin"] == ADANIENT
    assert marks["ADANIENT"]["resolved_via"] == ResolvedVia.SESSION.value
    assert marks["ADANIENT"]["nifty50_flag"] is True
    # The BSE row in the same partition is not an NSE statement.
    hits = {r["symbol"]: r for r in _read(built, BAND_HITS_DATASET)}
    assert hits["AKI"]["isin"] == AKI


def test_the_master_answers_only_where_the_session_is_silent(built: Path) -> None:
    marks = {r["symbol"]: r for r in _read(built, SECURITY_MARKS_DATASET) if r["series"] == "EQ"}
    assert marks["ARVIND"]["isin"] == ARVIND
    assert marks["ARVIND"]["resolved_via"] == ResolvedVia.MASTER.value
    assert marks["ARVIND"]["corp_ind"] == "XD"


def test_the_master_is_never_asked_about_a_debt_series(built: Path) -> None:
    """`1003SCL32` is in the master (as WRONG) but its Bc rows are N1/U1 debentures."""
    quarantined = [r for r in _read(built, QUARANTINE_DATASET) if r["symbol"] == "1003SCL32"]
    assert quarantined and {r["series"] for r in quarantined} <= {"N1", "U1"}
    assert {r["reason"] for r in quarantined} == {QuarantineReason.SYMBOL_UNRESOLVED.value}
    assert not [r for r in _read(built, CA_BROADCASTS_DATASET) if r["isin"] == WRONG]


def test_an_ambiguous_session_statement_is_quarantined_not_picked(built: Path) -> None:
    assert not [r for r in _read(built, SECURITY_MARKS_DATASET) if r["symbol"] == "AIAENG"]
    held = [r for r in _read(built, QUARANTINE_DATASET) if r["symbol"] == "AIAENG"]
    assert held and {r["reason"] for r in held} == {QuarantineReason.SYMBOL_UNRESOLVED.value}


def test_an_ambiguous_master_is_its_own_reason(tmp_path: Path) -> None:
    store = _store(tmp_path, (("pd/lowercase", "pd04092026.csv"),))
    _prices_raw(tmp_path, [("ADANIENT", "EQ", ADANIENT)])
    master = _master(("ARVIND", ARVIND), ("ARVIND", WRONG))
    rebuild_from_l0(start=SESSION, end=SESSION, l0=store, master=master, data_root=tmp_path)
    held = [r for r in _read(tmp_path, QUARANTINE_DATASET) if r["symbol"] == "ARVIND"]
    assert {r["reason"] for r in held} == {QuarantineReason.AMBIGUOUS_MASTER.value}


def test_no_session_statement_is_named_as_such(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = rebuild_from_l0(start=SESSION, end=SESSION, l0=store, master=None, data_root=tmp_path)
    assert report.sessions_without_statement == [SESSION.isoformat()]
    assert {r["reason"] for r in _read(tmp_path, QUARANTINE_DATASET)} == {
        QuarantineReason.NO_SESSION_STATEMENT.value
    }
    # Index rows need no identity and still land.
    assert len(_read(tmp_path, INDEX_EOD_DATASET)) == 139


def test_a_rebuild_is_byte_identical(built: Path) -> None:
    before = {d: l1_partition_path(d, SESSION, data_root=built).read_bytes() for d in PR_DATASETS}
    store = L0Store(clock=FrozenClock(SESSION), data_root=built)
    master = _master(("ADANIENT", WRONG), ("ARVIND", ARVIND), ("1003SCL32", WRONG))
    rebuild_from_l0(start=SESSION, end=SESSION, l0=store, master=master, data_root=built)
    after = {d: l1_partition_path(d, SESSION, data_root=built).read_bytes() for d in PR_DATASETS}
    assert before == after


def test_index_rows_carry_a_stable_id_across_renames(built: Path) -> None:
    rows = {r["index_name"]: r for r in _read(built, INDEX_EOD_DATASET)}
    assert rows["Nifty 50"]["index_id"] == "NIFTY 50"
    assert rows["India VIX"]["index_id"] == "INDIA VIX"
    assert index_id_for("S&P CNX Nifty") == index_id_for("CNX Nifty") == "NIFTY 50"
    assert index_id_for("CNX Nifty Junior") == "NIFTY NEXT 50"
    assert index_id_for("Nifty  Midcap 50") == "Nifty Midcap 50"
    # Two different series NSE published under one name in two cases must stay two ids.
    assert index_id_for("Nifty Midcap 100") != index_id_for("NIFTY MIDCAP 100")


def test_a_member_absent_from_the_bundle_removes_its_partition(built: Path) -> None:
    assert l1_partition_path(BAND_HITS_DATASET, SESSION, data_root=built).exists()
    # Re-publish the session's L0 under a second root carrying only pd, sharing L1 with `built`.
    other = built / "other"
    store = _store(other, (("pd/lowercase", "pd04092026.csv"),))
    rebuild_from_l0(start=SESSION, end=SESSION, l0=store, master=None, data_root=built)
    assert not l1_partition_path(BAND_HITS_DATASET, SESSION, data_root=built).exists()
    assert not l1_partition_path(CA_BROADCASTS_DATASET, SESSION, data_root=built).exists()
    assert l1_partition_path(SECURITY_MARKS_DATASET, SESSION, data_root=built).exists()


def test_an_undated_bundle_is_reported_and_writes_nothing(tmp_path: Path) -> None:
    session = date(2018, 1, 2)
    store = L0Store(clock=FrozenClock(session), data_root=tmp_path)
    store.put(
        "nse_pr_bundle",
        session,
        "PR020118.zip",
        (FIXTURES / "bh_misserved" / "PR020118.zip").read_bytes(),
    )
    report = rebuild_from_l0(start=session, end=session, l0=store, master=None, data_root=tmp_path)
    assert (report.bundles, report.built, len(report.undated)) == (1, 0, 1)
    assert not (tmp_path / "L1").exists() or not any((tmp_path / "L1").rglob("*.parquet"))
