"""M3.9.b — the published NIFTY total-return series (§4.1 row 8).

The acceptance this file discharges is the one D8 ratified on 2026-08-10, replacing the original
third bullet of M3.9 (which required a live fetch at verify time and so could never pass offline,
against rule B8). Every assertion below runs against frozen payloads in
`tests/fixtures/nifty_indices/tri/2026/` — real bytes, fetched 2026-09-08 at >=2.5 s spacing, never
the network:

1. **Three indices parse.** NIFTY 50, NIFTY IT and NIFTY CPSE over 2021-04-01..2026-03-31, 1,239
   rows each, with identical date sets.
2. **Dates normalise.** Rows arrive newest-first and come back strictly ascending, no duplicate,
   and with no *gap* against the M1.7 trading calendar — every calendar session in the window has a
   published level.
3. **The literal published levels.** `33655.43` / `41606.83` / `11793.29` on 2026-03-30, as
   `Decimal`, matching the levels the owner recorded independently from the live endpoint in
   HUMAN_DECISIONS D8. This is M3.9's third criterion — "spot-checked against a published value" —
   discharged offline for the first time.
4. **Every level is `Decimal` and positive, and `NTR_Value` is `None`, never `Decimal(0)`, where
   the source publishes `'-'`.** A JSON *number* in the level field is refused outright, because
   the only way to accept one is through a float.

Two further properties are asserted here because they are what a plausible wrong implementation
gets wrong:

* **`knowable_date` is derived from the data, never from a clock** — the defect at
  `dataplatform/ingest/bse/corp_actions.py:214`, which stamped every one of 47,887 corporate
  actions with the ingest date and made invariant #7 unfalsifiable. A 1999 level parsed today is
  knowable in 1999.
* **The three observed endpoint traps** (newest-first rows, a title-cased index-name echo, and a
  `RequestNumber` that regenerates per request) are handled in the parser, not left to callers.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.ingest.calendar import expected_data_dates
from dataplatform.ingest.indices import (
    TRI_DATASET,
    TRI_METHOD_COMPUTED,
    TRI_METHOD_PUBLISHED,
    TRI_PUBLICATION_LAG_DAYS,
    TriPoint,
    TriSeries,
    l0_tri_filename,
    parse_tri_native,
    read_tri_series,
    tri_knowable_date,
    tri_request_body,
    tri_state_source,
    write_tri_l1,
)
from dataplatform.ingest.models import IngestError, ParseError
from dataplatform.store.paths import Layer, layer_root

FIXTURES: Final = Path("tests/fixtures/nifty_indices/tri/2026")

#: The window D8's ratified acceptance names, and the three indices the reference case needs
#: (AGENTIC_CONTEXT §8's reference-case fixture is NIFTY 50 + NIFTY IT + NIFTY CPSE).
WINDOW_START: Final = date(2021, 4, 1)
WINDOW_END: Final = date(2026, 3, 31)
SPOT_DATE: Final = date(2026, 3, 30)

#: `(slug, name as sent in CAPS, the published level on SPOT_DATE)` — the levels are D8's, recorded
#: by the owner from the live endpoint on 2026-08-10 and reproduced by these frozen payloads.
INDICES: Final = (
    ("nifty50", "NIFTY 50", Decimal("33655.43")),
    ("niftyit", "NIFTY IT", Decimal("41606.83")),
    ("niftycpse", "NIFTY CPSE", Decimal("11793.29")),
)


def _payload(slug: str) -> bytes:
    return (FIXTURES / l0_tri_filename(slug, WINDOW_START, WINDOW_END)).read_bytes()


def _series(slug: str, name: str) -> TriSeries:
    return parse_tri_native(
        _payload(slug),
        filename=l0_tri_filename(slug, WINDOW_START, WINDOW_END),
        index_name=name,
        index_slug=slug,
    )


@pytest.fixture(scope="module")
def parsed() -> dict[str, TriSeries]:
    """The three published series, parsed once — 3,717 points is enough to be worth caching."""
    return {slug: _series(slug, name) for slug, name, _ in INDICES}


# ── acceptance (i): three indices, 1,239 rows each, identical date sets ────────────────────────


def test_three_indices_parse_with_identical_date_sets(parsed: dict[str, TriSeries]) -> None:
    """All three indices publish on the same 1,239 sessions over the window (D8 criterion i)."""
    date_sets = {slug: {point.as_of for point in series.points} for slug, series in parsed.items()}
    for slug, dates in date_sets.items():
        assert len(parsed[slug].points) == 1239, slug
        assert len(dates) == 1239, slug
    assert date_sets["nifty50"] == date_sets["niftyit"] == date_sets["niftycpse"]


def test_each_series_is_its_own_index_and_the_published_method(
    parsed: dict[str, TriSeries],
) -> None:
    """The slug is the caller's canonical one; the echoed name is title-cased, not what was sent."""
    assert parsed["nifty50"].index_name == "Nifty 50"
    assert parsed["niftyit"].index_name == "Nifty IT"
    assert parsed["niftycpse"].index_name == "Nifty CPSE"
    for slug, series in parsed.items():
        assert series.index_slug == slug
        assert series.method == TRI_METHOD_PUBLISHED


# ── acceptance (ii): dates normalise — ascending, no duplicate, no gap ────────────────────────


def test_rows_arrive_newest_first_and_come_back_ascending(parsed: dict[str, TriSeries]) -> None:
    """The endpoint's own order is newest-first; a parser that trusted it would invert every return.

    Asserted against the raw payload rather than assumed, so the day the endpoint changes order
    this test says which direction it changed.
    """
    raw = json.loads(_payload("nifty50"))
    assert raw[0]["Date"] == "30 Mar 2026"
    assert raw[-1]["Date"] == "01 Apr 2021"

    for series in parsed.values():
        dates = [point.as_of for point in series.points]
        assert dates == sorted(dates)
        assert len(dates) == len(set(dates))


def test_no_gap_against_the_trading_calendar(parsed: dict[str, TriSeries]) -> None:
    """Every M1.7 calendar session in the window has a published level (D8 criterion ii).

    The containment is one-directional on purpose. The published series is a *superset* of the
    checked-in calendar's sessions: it also carries the five Budget/DR-site Saturday sessions
    (2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01, 2026-02-01) that `expected_data_dates` does
    not model. Those are real sessions the exchange traded and priced, so a level on one is not a
    defect in the TRI — it is a hole in the calendar, recorded here rather than papered over. What
    *would* be a defect is a calendar session with no level, and there is none.
    """
    calendar = set(expected_data_dates(WINDOW_START, WINDOW_END))
    assert calendar, "the checked-in calendar must cover the acceptance window"
    for slug, series in parsed.items():
        published = {point.as_of for point in series.points}
        assert not calendar - published, f"{slug}: sessions with no published level"
        assert published - calendar == {
            date(2024, 1, 20),
            date(2024, 3, 2),
            date(2024, 5, 18),
            date(2025, 2, 1),
            date(2026, 2, 1),
        }, f"{slug}: unexpected extra sessions"


# ── acceptance (iii): the literal published levels — M3.9's never-satisfied spot-check ─────────


@pytest.mark.parametrize(("slug", "name", "level"), INDICES)
def test_published_level_on_the_spot_check_date(
    parsed: dict[str, TriSeries], slug: str, name: str, level: Decimal
) -> None:
    """The literal `Decimal` level on 2026-03-30, matching what D8 recorded from the live endpoint.

    Exact equality, not `pytest.approx`: a published index level is a decimal string on the wire
    and a `Decimal` in the store, and any tolerance here would hide the float that a tolerance
    exists to accommodate.
    """
    point = next(p for p in parsed[slug].points if p.as_of == SPOT_DATE)
    assert point.tri_value == level
    assert isinstance(point.tri_value, Decimal)


# ── acceptance (iv): Decimal everywhere, None-not-zero for '-' ─────────────────────────────────


def test_every_level_is_a_positive_decimal(parsed: dict[str, TriSeries]) -> None:
    for slug, series in parsed.items():
        for point in series.points:
            assert isinstance(point.tri_value, Decimal), slug
            assert point.tri_value > 0, slug
            assert point.ntr_value is None or isinstance(point.ntr_value, Decimal)


def test_absent_ntr_value_is_none_never_zero(parsed: dict[str, TriSeries]) -> None:
    """`NTR_Value` is `'-'` for every NIFTY IT and NIFTY CPSE row — absent, not zero.

    Zero would read as "the net total return was flat", which is a different and false claim; a
    consumer filtering on `> 0` would silently drop the whole series instead of seeing it is not
    published.
    """
    for slug in ("niftyit", "niftycpse"):
        values = {point.ntr_value for point in parsed[slug].points}
        assert values == {None}, slug

    nifty50 = {point.as_of: point.ntr_value for point in parsed["nifty50"].points}
    assert nifty50[SPOT_DATE] == Decimal("29316.37")


def test_a_json_number_level_is_refused_rather_than_coerced() -> None:
    """A bare JSON number cannot be read without a float, so it raises (CLAUDE.md's money rule)."""
    payload = json.dumps(
        [{"Index Name": "Nifty 50", "Date": "30 Mar 2026", "TotalReturnsIndex": 33655.43}]
    ).encode()
    with pytest.raises(ParseError, match="not a decimal string"):
        parse_tri_native(payload, filename="n.json", index_name="NIFTY 50", index_slug="nifty50")


# ── the PIT boundary: derived from the data, never from a clock ────────────────────────────────


def test_knowable_date_is_the_session_not_the_ingest_date(parsed: dict[str, TriSeries]) -> None:
    """A level dated D is knowable on D — the publication schedule, not "today".

    This is the property `dataplatform/ingest/bse/corp_actions.py:214` gets wrong: it stamps
    `knowable_date=clock.now().date()`, so all 47,887 corporate actions claim to have become
    knowable on the day they were loaded. A PIT filter reading that column can never find a
    violation, which satisfies invariant #7 vacuously. Here the boundary is a pure function of the
    row's own published date, so a 2021 level is knowable in 2021 no matter when it is parsed.
    """
    assert TRI_PUBLICATION_LAG_DAYS == 0
    for series in parsed.values():
        for point in series.points:
            assert point.knowable_date == point.as_of
            assert point.knowable_date == tri_knowable_date(point.as_of)

    earliest = min(point.as_of for point in parsed["nifty50"].points)
    assert earliest == WINDOW_START
    assert tri_knowable_date(earliest) == WINDOW_START


def test_knowable_date_ignores_a_wildly_wrong_clock() -> None:
    """Parsing under a clock set decades ahead moves no level's knowable date.

    `FrozenClock` is the injected clock the rest of D1 uses (B10). The parser never takes one — the
    point of this test is that it *cannot*, so there is no path by which an ingest run's wall time
    reaches a stored PIT boundary.
    """
    clock = FrozenClock(date(2099, 1, 1))
    assert clock.today() == date(2099, 1, 1)
    series = _series("nifty50", "NIFTY 50")
    assert max(point.knowable_date for point in series.points) == SPOT_DATE


def test_knowable_date_cannot_be_set_by_a_caller() -> None:
    """`knowable_date` is a computed field; passing one is an error, not an override."""
    with pytest.raises(ValueError, match="knowable_date"):
        TriPoint(
            index_slug="nifty50",
            index_name="Nifty 50",
            as_of=SPOT_DATE,
            tri_value=Decimal("33655.43"),
            method=TRI_METHOD_PUBLISHED,
            knowable_date=date(2026, 9, 8),  # type: ignore[call-arg]
        )


# ── the endpoint's traps, and the failure shapes that must stay loud ──────────────────────────


def test_request_number_reaches_nothing_that_is_stored(parsed: dict[str, TriSeries]) -> None:
    """`RequestNumber` regenerates per request, so nothing derived from the payload may carry it.

    It is in the raw records (asserted, so the trap stays documented by a test rather than by
    folklore) and in no field of the parsed series — otherwise two fetches of the same history
    would produce different L1 bytes and break "same inputs → byte-identical".
    """
    raw = json.loads(_payload("nifty50"))
    assert raw[0]["RequestNumber"].startswith("TRI")
    dumped = parsed["nifty50"].model_dump_json()
    assert "RequestNumber" not in dumped
    assert raw[0]["RequestNumber"] not in dumped


def test_the_stale_paths_html_never_becomes_a_benchmark() -> None:
    """`Backpage.aspx/...` answers 200 with the site's home page — and `Content-Type: text/html`.

    The success response is *also* `text/html`, so a content-type check cannot discriminate (D9).
    Only this shape assertion can, which is why it raises rather than warning.
    """
    with pytest.raises(ParseError, match="markup, not JSON"):
        parse_tri_native(
            b"<!DOCTYPE html><html>login</html>",
            filename="gate.html",
            index_name="NIFTY 50",
            index_slug="nifty50",
        )


def test_an_object_body_is_refused_with_the_shape_it_expected() -> None:
    """The pre-D8 parser expected an ASP.NET `{"d": …}` envelope. There is no envelope."""
    with pytest.raises(ParseError, match="bare array"):
        parse_tri_native(
            b'{"d": "[]"}', filename="d.json", index_name="NIFTY 50", index_slug="nifty50"
        )


def test_a_payload_about_another_index_is_refused() -> None:
    """The endpoint echoes the name back; a mismatch must not be filed under the wrong slug."""
    payload = json.dumps(
        [{"Index Name": "Nifty Bank", "Date": "30 Mar 2026", "TotalReturnsIndex": "58000.00"}]
    ).encode()
    with pytest.raises(ParseError, match="different index"):
        parse_tri_native(payload, filename="x.json", index_name="NIFTY 50", index_slug="nifty50")


def test_two_levels_for_one_date_are_refused() -> None:
    record = {"Index Name": "Nifty 50", "Date": "30 Mar 2026", "TotalReturnsIndex": "33655.43"}
    with pytest.raises(ParseError, match="two levels published"):
        parse_tri_native(
            json.dumps([record, record]).encode(),
            filename="dup.json",
            index_name="NIFTY 50",
            index_slug="nifty50",
        )


# ── the request envelope ──────────────────────────────────────────────────────────────────────


def test_request_body_is_the_single_quoted_cinfo_string() -> None:
    """`cinfo`'s value is itself a string, and its inner object is single-quoted — not JSON."""
    body = tri_request_body("NIFTY 50", WINDOW_START, WINDOW_END)
    envelope = json.loads(body)
    assert set(envelope) == {"cinfo"}
    assert envelope["cinfo"] == (
        "{'name':'NIFTY 50','startDate':'01-Apr-2021',"
        "'endDate':'31-Mar-2026','indexName':'NIFTY 50'}"
    )


def test_request_body_insists_on_caps() -> None:
    """The endpoint is case-sensitive inbound; a title-cased name is a silent empty result."""
    with pytest.raises(IngestError, match="CAPS"):
        tri_request_body("Nifty 50", WINDOW_START, WINDOW_END)


def test_request_body_refuses_a_name_that_would_reshape_the_envelope() -> None:
    with pytest.raises(IngestError, match="reshape"):
        tri_request_body("NIFTY 50','X':'", WINDOW_START, WINDOW_END)


def test_request_body_refuses_an_inverted_window() -> None:
    with pytest.raises(IngestError, match="ends"):
        tri_request_body("NIFTY 50", WINDOW_END, WINDOW_START)


def test_l0_filename_carries_the_index_and_the_window() -> None:
    """One URL serves every index and window, so the L0 name must separate them (`L0Store.put`)."""
    assert (
        l0_tri_filename("nifty50", WINDOW_START, WINDOW_END) == "tri_nifty50_20210401_20260331.json"
    )
    assert l0_tri_filename("niftyit", WINDOW_START, WINDOW_END).startswith("tri_niftyit_")


def test_sync_source_is_qualified_per_index() -> None:
    """Three indices on one logical date would otherwise collide on one `(source, date)` row."""
    assert tri_state_source("nifty50") == "nifty_tri_history/nifty50"
    assert tri_state_source("niftyit") != tri_state_source("nifty50")


# ── L1: the published series and the computed estimate are different files ────────────────────


def test_published_and_computed_series_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """Both methods for one index on one date coexist, and each reads back as itself.

    A single `<slug>.parquet` per partition would have let whichever ingest ran second silently
    replace the other — the estimate overwriting the exchange's own series, or the reverse,
    depending on the order the two runners happened to fire.
    """
    published = _series("nifty50", "NIFTY 50")
    computed = TriSeries(
        index_slug="nifty50",
        index_name="Nifty 50",
        method=TRI_METHOD_COMPUTED,
        points=(
            TriPoint(
                index_slug="nifty50",
                index_name="Nifty 50",
                as_of=SPOT_DATE,
                tri_value=Decimal("30000.0000"),
                price_close=Decimal("29000.0000"),
                method=TRI_METHOD_COMPUTED,
            ),
        ),
    )
    write_tri_l1(published, data_root=tmp_path)
    write_tri_l1(computed, data_root=tmp_path)

    partition = layer_root(Layer.L1, data_root=tmp_path) / TRI_DATASET / f"date={SPOT_DATE}"
    assert {path.name for path in partition.iterdir()} == {
        "nifty50.published.parquet",
        "nifty50.computed.parquet",
    }

    only_published = read_tri_series(
        "nifty50", SPOT_DATE, method=TRI_METHOD_PUBLISHED, data_root=tmp_path
    )
    assert only_published is not None
    assert only_published.points[-1].tri_value == Decimal("33655.43")

    only_computed = read_tri_series(
        "nifty50", SPOT_DATE, method=TRI_METHOD_COMPUTED, data_root=tmp_path
    )
    assert only_computed is not None
    assert only_computed.points[-1].tri_value == Decimal("30000.0000")


def test_an_unqualified_read_prefers_the_published_series(tmp_path: Path) -> None:
    """With both on disk, a caller that does not name a method gets the exchange's own series.

    Invert the preference order and this fails — which is the point: the six years of M9/M10/M12
    benchmark figures were struck against the estimate precisely because nothing ever preferred
    the real series.
    """
    write_tri_l1(_series("nifty50", "NIFTY 50"), data_root=tmp_path)
    write_tri_l1(
        TriSeries(
            index_slug="nifty50",
            index_name="Nifty 50",
            method=TRI_METHOD_COMPUTED,
            points=(
                TriPoint(
                    index_slug="nifty50",
                    index_name="Nifty 50",
                    as_of=SPOT_DATE,
                    tri_value=Decimal("30000.0000"),
                    method=TRI_METHOD_COMPUTED,
                ),
            ),
        ),
        data_root=tmp_path,
    )
    series = read_tri_series("nifty50", SPOT_DATE, data_root=tmp_path)
    assert series is not None
    assert series.method == TRI_METHOD_PUBLISHED


def test_an_unqualified_read_falls_back_to_the_estimate_when_that_is_all_there_is(
    tmp_path: Path,
) -> None:
    write_tri_l1(
        TriSeries(
            index_slug="nifty50",
            index_name="Nifty 50",
            method=TRI_METHOD_COMPUTED,
            points=(
                TriPoint(
                    index_slug="nifty50",
                    index_name="Nifty 50",
                    as_of=SPOT_DATE,
                    tri_value=Decimal("30000.0000"),
                    method=TRI_METHOD_COMPUTED,
                ),
            ),
        ),
        data_root=tmp_path,
    )
    series = read_tri_series("nifty50", SPOT_DATE, data_root=tmp_path)
    assert series is not None
    assert series.method == TRI_METHOD_COMPUTED
    assert (
        read_tri_series("nifty50", SPOT_DATE, method=TRI_METHOD_PUBLISHED, data_root=tmp_path)
        is None
    )


def test_the_stored_pit_boundary_is_not_editable(tmp_path: Path) -> None:
    """A hand-edited `knowable_date` column raises on read rather than moving the boundary."""
    import pyarrow.parquet as pq

    write_tri_l1(_series("nifty50", "NIFTY 50"), data_root=tmp_path)
    path = (
        layer_root(Layer.L1, data_root=tmp_path)
        / TRI_DATASET
        / f"date={SPOT_DATE}"
        / "nifty50.published.parquet"
    )
    table = pq.read_table(path)
    edited = table.set_column(
        table.schema.get_field_index("knowable_date"),
        table.schema.field("knowable_date"),
        [[date(1999, 1, 1)]],
    )
    pq.write_table(edited, path, compression="snappy", version="2.6")
    with pytest.raises(IngestError, match="not editable"):
        read_tri_series("nifty50", SPOT_DATE, method=TRI_METHOD_PUBLISHED, data_root=tmp_path)
