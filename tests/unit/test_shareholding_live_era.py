"""M13.3 — the shareholding parser against the live master (`master_2026_10` era), and D17.

The `json_v1` suite (`test_shareholding.py`) was built on a synthetic fixture. The live master
captured on 2026-10-06 differs in four ways that each break a naive parser, and this file is laid
out as those four plus D17's rule:

1. **Both eras parse.** The live payload says `public_val` where v1 said `public_prcnt`, carries no
   pledge and no FII/DII split, and adds `employeeTrusts` and a `revisedData` marker. The frozen
   live fixture is the L0 capture byte for byte (its sha256 is asserted), so "parses the fixture"
   means "parses what NSE actually served".
2. **Pledge absent → BC3 `not_applicable`, explicitly.** Not `clear`, not a skipped row: every
   such row raises an INFO `quality_flag` under `shareholding_bc3`, which is what puts the gap on
   `/status/quality`.
3. **Pledge present and bad still fails BC3** — in the live era too. A pledge above 50% is a BREACH
   and a WARN finding; exactly 50 is CLEAR. Inverting the comparison, or reading a present-but-
   garbled pledge as "absent", fails these.
4. **ISIN is the only join key.** A record NSE published with `isin: null` is skipped and counted,
   with a WARN finding naming its symbol — never resolved from that symbol.
5. **A malformed payload fails loud** — a mixed-era payload, a record of neither era, an unknown
   revision marker, a record with no `isin` key at all, or the live fixture truncated mid-array.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.shareholding import (
    BC3_CHECK,
    NO_ISIN_CHECK,
    PLEDGE_BREACH_PCT,
    SOURCE_ID,
    Bc3Status,
    ShareholdingSnapshot,
    parse,
    quality_findings,
    read_l1,
    read_pit,
    write_l1,
)

LIVE: Final = Path(
    "tests/fixtures/nse_shareholding/master_2026_10/corporate-share-holdings-master_20261006.json"
)
V1: Final = Path(
    "tests/fixtures/nse_shareholding/json_v1/corporate-share-holdings-master_20260807.json"
)
#: The sha256 L0 recorded for this payload at capture (`.meta.json`), and PROVENANCE.md repeats.
LIVE_SHA256: Final = "6bed2ff4c3a38b161a1dd78ff0d75e6f3423cb8087fe4e2b3929fc108af9faf5"
LIVE_KEY: Final = f"{SOURCE_ID}/2026-10-06/{LIVE.name}"

IMFA: Final = "INE919H01018"  # Indian Metals & Ferro Alloys — filed 06-Oct for 30-Sep
A_ONE_STEELS: Final = "INE0OTC01025"  # the one revised filing
EMPLOYEE_TRUST_HOLDER_PCT: Final = Decimal("0.7")


@pytest.fixture(scope="module")
def live_bytes(repo_root: Path) -> bytes:
    return (repo_root / LIVE).read_bytes()


@pytest.fixture(scope="module")
def live(live_bytes: bytes) -> ShareholdingSnapshot:
    return parse(live_bytes, filename=LIVE.name, l0_key=LIVE_KEY)


def _live_record(**overrides: Any) -> dict[str, Any]:
    """One live-era record (IMFA's, as served), mutated per test. `None` deletes a key."""
    base: dict[str, Any] = {
        "broadcastDate": "06-OCT-2026 10:33:01",
        "cgTimeStamp": None,
        "date": "30-SEP-2026",
        "employeeTrusts": "0",
        "isin": IMFA,
        "name": "Indian Metals & Ferro Alloys Limited",
        "pr_and_prgrp": "58.69",
        "public_val": "41.31",
        "revisedData": "N",
        "submissionDate": "06-OCT-2026",
        "symbol": "IMFA",
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


def _body(*records: dict[str, Any]) -> bytes:
    return json.dumps(list(records)).encode("utf-8")


# ── 1. both eras parse ────────────────────────────────────────────────────────────────────────


def test_the_live_fixture_is_the_l0_capture_byte_for_byte(live_bytes: bytes) -> None:
    """Provenance, checked: the frozen era is exactly what L0 holds, not a hand-edited copy."""
    assert hashlib.sha256(live_bytes).hexdigest() == LIVE_SHA256


def test_both_format_eras_parse(repo_root: Path, live: ShareholdingSnapshot) -> None:
    v1 = parse((repo_root / V1).read_bytes(), filename=V1.name)
    assert len(v1.rows) == 5
    assert len(live.rows) == 30
    assert len(live.skipped_no_isin) == 2


def test_the_live_era_reads_its_own_keys(live: ShareholdingSnapshot) -> None:
    imfa = next(row for row in live.rows if row.isin == IMFA)
    assert imfa.period_end == date(2026, 9, 30)
    assert imfa.filing_date == date(2026, 10, 6)  # from broadcastDate; cgTimeStamp is null
    assert imfa.promoter_holding_pct == Decimal("58.69")
    assert imfa.public_pct == Decimal("41.31")  # `public_val`, not `public_prcnt`
    assert imfa.employee_trusts_pct == Decimal("0")
    assert imfa.fii_pct is None and imfa.dii_pct is None
    assert imfa.revised is False


def test_the_revision_marker_and_employee_trusts_are_carried(live: ShareholdingSnapshot) -> None:
    revised = [row.isin for row in live.rows if row.revised]
    assert revised == [A_ONE_STEELS]
    trusts = [row for row in live.rows if row.employee_trusts_pct]
    assert [row.employee_trusts_pct for row in trusts] == [EMPLOYEE_TRUST_HOLDER_PCT]
    # The three buckets close to 100 on the one company that has all three.
    only = trusts[0]
    assert only.promoter_holding_pct + only.public_pct + EMPLOYEE_TRUST_HOLDER_PCT == Decimal(100)


def test_every_live_row_files_after_its_quarter(live: ShareholdingSnapshot) -> None:
    for row in live.rows:
        assert row.period_end == date(2026, 9, 30)
        assert row.filing_date > row.period_end


# ── 2. pledge absent → BC3 not_applicable, explicitly ─────────────────────────────────────────


def test_a_row_without_a_pledge_is_not_applicable_never_clear(live: ShareholdingSnapshot) -> None:
    for row in live.rows:
        assert row.promoter_pledge_pct is None
        assert row.bc3_status is Bc3Status.NOT_APPLICABLE
    assert live.breaching() == ()
    assert len(live.bc3_not_applicable()) == len(live.rows)


def test_every_not_applicable_row_is_an_info_finding_for_the_status_api(
    live: ShareholdingSnapshot,
) -> None:
    """D17's "never silently skipped": each un-evaluated row is a `quality_flag`, per ISIN."""
    bc3 = [f for f in quality_findings(live) if f.check_name == BC3_CHECK]
    assert {f.isin for f in bc3} == {row.isin for row in live.rows}
    for finding in bc3:
        assert finding.severity == "INFO"
        assert finding.source == SOURCE_ID
        assert finding.detail["bc3_status"] == "not_applicable"
        assert finding.observed_value is None
        assert finding.logical_date >= date(2026, 10, 1)  # dated by filing, i.e. knowable date


def test_a_null_pledge_survives_l1_as_null_not_zero(
    live: ShareholdingSnapshot, tmp_path: Path
) -> None:
    """A zero pledge would read back as CLEAR — the silent skip, reintroduced by the store."""
    write_l1(live, data_root=tmp_path)
    back = read_l1(date(2026, 10, 6), data_root=tmp_path)
    assert back
    for row in back:
        assert row.promoter_pledge_pct is None
        assert row.bc3_status is Bc3Status.NOT_APPLICABLE
    revised = next(row for row in read_pit(date(2026, 10, 6), data_root=tmp_path) if row.revised)
    assert revised.isin == A_ONE_STEELS


def test_an_explicit_null_pledge_is_absent_too() -> None:
    body = _body(_live_record() | {"pledgeShares_prcnt": None})
    row = parse(body, filename="x.json").rows[0]
    assert row.bc3_status is Bc3Status.NOT_APPLICABLE


# ── 3. pledge present and bad still fails BC3 ─────────────────────────────────────────────────


def test_a_live_era_pledge_over_the_threshold_breaches_and_warns() -> None:
    snapshot = parse(_body(_live_record(pledgeShares_prcnt="62.50")), filename="x.json")
    row = snapshot.rows[0]
    assert row.bc3_status is Bc3Status.BREACH
    assert snapshot.breaching() == (row,)
    (finding,) = quality_findings(snapshot)
    assert finding.check_name == BC3_CHECK
    assert finding.severity == "WARN"
    assert finding.detail["bc3_status"] == "breach"
    assert finding.observed_value == Decimal("62.50")
    assert finding.threshold == PLEDGE_BREACH_PCT


@pytest.mark.parametrize(
    ("pledge", "expected"),
    [
        ("0.00", Bc3Status.CLEAR),
        ("50.00", Bc3Status.CLEAR),
        ("50.01", Bc3Status.BREACH),
        ("100", Bc3Status.BREACH),
    ],
)
def test_the_bc3_boundary_is_strict_in_the_live_era(pledge: str, expected: Bc3Status) -> None:
    """Inverting `>` (or making it `>=`) moves at least one of these to the wrong side."""
    snapshot = parse(_body(_live_record(pledgeShares_prcnt=pledge)), filename="x.json")
    assert snapshot.rows[0].bc3_status is expected
    assert bool(quality_findings(snapshot)) is (expected is Bc3Status.BREACH)


@pytest.mark.parametrize("bad", ["", "   ", "n/a", "NaN", "Infinity", "120.00", "-1"])
def test_a_present_but_malformed_pledge_fails_the_parse_rather_than_reading_as_absent(
    bad: str,
) -> None:
    """A garbled pledge read as "absent" would hide the breach behind a quiet not_applicable."""
    with pytest.raises(ParseError, match=r"pledgeShares_prcnt|promoter_pledge_pct"):
        parse(_body(_live_record(pledgeShares_prcnt=bad)), filename="x.json")


# ── 4. ISIN is the only join key ──────────────────────────────────────────────────────────────


def test_a_record_published_without_an_isin_is_skipped_counted_and_flagged(
    live: ShareholdingSnapshot,
) -> None:
    skipped = {s.symbol for s in live.skipped_no_isin}
    assert skipped == {"CHENNPETRO", "HLVLTD"}
    names = {s.name for s in live.skipped_no_isin}
    assert not {row.name for row in live.rows} & names  # not in L1 rows under any identity
    flags = [f for f in quality_findings(live) if f.check_name == NO_ISIN_CHECK]
    assert {f.detail["symbol"] for f in flags} == skipped
    for flag in flags:
        assert flag.severity == "WARN"
        assert flag.isin is None
    assert len({f.fingerprint for f in quality_findings(live)}) == len(quality_findings(live))


def test_a_blank_isin_is_withheld_but_a_malformed_one_fails() -> None:
    assert parse(_body(_live_record(isin="  ")), filename="x.json").skipped_no_isin
    with pytest.raises(ParseError, match="isin"):
        parse(_body(_live_record(isin="IMFA")), filename="x.json")


# ── 5. a malformed payload fails loud ─────────────────────────────────────────────────────────


def test_a_record_with_no_isin_key_at_all_is_a_format_error() -> None:
    with pytest.raises(ParseError, match="no 'isin' field"):
        parse(_body(_live_record(isin=None)), filename="x.json")


def test_a_payload_mixing_eras_is_rejected(repo_root: Path) -> None:
    v1_record = json.loads((repo_root / V1).read_bytes())[0]
    with pytest.raises(ParseError, match="one payload is one format era"):
        parse(_body(_live_record(), v1_record), filename="x.json")


def test_a_record_of_neither_era_names_what_it_has() -> None:
    with pytest.raises(ParseError, match="public-holding key"):
        parse(_body(_live_record(public_val=None)), filename="x.json")
    with pytest.raises(ParseError, match="public-holding key"):
        parse(_body(_live_record(public_prcnt="41.31")), filename="x.json")


def test_an_unknown_revision_marker_is_rejected() -> None:
    with pytest.raises(ParseError, match="revisedData"):
        parse(_body(_live_record(revisedData="Y")), filename="x.json")


def test_the_live_fixture_truncated_fails_loud(live_bytes: bytes) -> None:
    with pytest.raises(ParseError, match="not valid JSON"):
        parse(live_bytes[: len(live_bytes) // 2], filename=LIVE.name)


def test_quality_findings_are_deterministic(live_bytes: bytes) -> None:
    first = quality_findings(parse(live_bytes, filename=LIVE.name, l0_key=LIVE_KEY))
    second = quality_findings(parse(live_bytes, filename=LIVE.name, l0_key=LIVE_KEY))
    assert first == second
