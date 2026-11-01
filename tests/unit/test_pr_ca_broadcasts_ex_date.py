"""L1 `pr_ca_broadcasts` marks an ex-date it derived — on the real AvenuesAI bc line.

`bc12102026.csv` (L0 `nse_pr_bundle/2026/10/PR121026.zip`) leaves EX_DT blank on AvenuesAI's
``CNSLDATNRE1 TO RS10`` with record date 2026-10-13; BANSALWIRE's split on the next line states
its own. The L1 build fills the first by the T+1 rule and names the rule in `ex_date_derived`; the
second is carried as published, with no rule.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from dataplatform.clock import FrozenClock
from dataplatform.ingest.pr_bundle_l1 import CA_BROADCASTS_DATASET, rebuild_from_l0
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "share_basis" / "bc12102026.csv"
SESSION = date(2026, 10, 12)


def _bundle() -> bytes:
    body = "\r\n".join(
        line for line in FIXTURE.read_text().splitlines() if not line.startswith("#")
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(zipfile.ZipInfo("bc12102026.csv", date_time=(2026, 10, 12, 18, 0, 0)), body)
    return buffer.getvalue()


def test_a_blank_t1_face_value_ex_date_is_derived_and_marked(tmp_path: Path) -> None:
    store = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    store.put("nse_pr_bundle", SESSION, "PR121026.zip", _bundle())
    path = l1_partition_path("prices_raw", SESSION, data_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES "
        "('INE483S01020', 'NSE', 'CCAVENUE', 'EQ', DATE '2026-10-12'), "
        "('INE0B9K01025', 'NSE', 'BANSALWIRE', 'EQ', DATE '2026-10-12')"
        ") v(isin, exchange, symbol, series, trade_date)"
    )
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    con.close()

    report = rebuild_from_l0(start=SESSION, end=SESSION, l0=store, master=None, data_root=tmp_path)
    assert report.built == 1 and not report.failures
    rows = {
        r["symbol"]: r
        for r in pq.read_table(
            l1_partition_path(CA_BROADCASTS_DATASET, SESSION, data_root=tmp_path)
        ).to_pylist()
    }
    assert rows["CCAVENUE"]["ex_date"] == date(2026, 10, 13)
    assert rows["CCAVENUE"]["ex_date_derived"] == "T1_RECORD_DATE"
    assert rows["CCAVENUE"]["record_date"] == date(2026, 10, 13)
    assert rows["BANSALWIRE"]["ex_date"] == date(2026, 10, 16)
    assert rows["BANSALWIRE"]["ex_date_derived"] is None
