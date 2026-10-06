"""The September 2021 review, read by eye: provenance, balance, cross-checks, and the PIT boundary.

`ind_prs23082021.pdf` has no text layer, so its transcription is the only reading of it. These tests
hold it to what the release itself must satisfy rather than to a second copy of the rows:

1. **Provenance.** The entry pins the L0 object by key and sha256, says who transcribed it and when,
   and `transcription_parse` refuses bytes that are not that object.
2. **Consistency, inside and out.** Every complete tracked section balances (a fixed-size index
   replaces like for like); NIFTY 100's table equals NIFTY Next 50's (NIFTY 50 is stated
   unchanged); NIFTY 200's equals the net of NIFTY 100's and Midcap 100's. Against the text release
   that re-issued three of the lists three weeks later (ind_prs15092021.pdf, a frozen fixture),
   every transcribed row agrees except exactly the changes that release announces.
3. **The PIT boundary.** The walk applied to the transcribed Next 50 change switches the set on
   2021-09-30 — not on the "close of" date, not on the announcement — so shifting the effective date
   or letting the announcement leak the new set fails a named assertion.

Offline: the lake is consulted only to confirm the pinned sha, and skipped where there is none.
"""

from __future__ import annotations

import json
import zlib
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import pytest

from dataplatform.ingest.index_changes import (
    TRACKED_INDICES,
    ChangeAction,
    IndexChangeEvent,
    parse_press_release_pdf,
)
from dataplatform.ingest.index_history import ResolvedEvent, reconstruct_index
from dataplatform.ingest.index_transcription import (
    TRANSCRIPTIONS_PATH,
    ReleaseTranscription,
    TranscribedSection,
    TranscriptionError,
    load_release_transcriptions,
    transcription_parse,
)
from dataplatform.ingest.indices import ConstituentRow, ConstituentSnapshot
from dataplatform.ingest.models import ParseError
from dataplatform.store.l0 import L0Ref

RELEASE: Final = "ind_prs23082021.pdf"
REVISION: Final = Path(
    "tests/fixtures/nifty_index_press_releases/2021_replaced_lists/ind_prs15092021.pdf"
)
EFFECTIVE: Final = date(2021, 9, 30)
ANNOUNCED: Final = date(2021, 8, 23)
_LAKE: Final = Path("/home/ubuntu/stock-manager/data/L0")


@pytest.fixture(scope="module")
def sept2021() -> ReleaseTranscription:
    return load_release_transcriptions()[RELEASE]


@pytest.fixture(scope="module")
def revision(repo_root: Path) -> tuple[IndexChangeEvent, ...]:
    path = repo_root / REVISION
    parsed = parse_press_release_pdf(
        path.read_bytes(), filename=path.name, announced=date(2021, 9, 15)
    )
    assert parsed.unparsed == ()
    return parsed.events


def _symbols(events: tuple[IndexChangeEvent, ...], slug: str, action: ChangeAction) -> set[str]:
    return {e.symbol or "" for e in events if e.index_slug == slug and e.action is action}


def _section(t: ReleaseTranscription, slug: str) -> TranscribedSection:
    section = t.section(slug)
    assert section is not None, slug
    return section


def _net(*sections: TranscribedSection) -> tuple[set[str], set[str]]:
    """(excluded, included) of a disjoint union, from its components' changes."""
    count: Counter[str] = Counter()
    for s in sections:
        count.update(r.symbol for r in s.include)
        count.subtract(r.symbol for r in s.exclude)
    return {k for k, v in count.items() if v < 0}, {k for k, v in count.items() if v > 0}


def _ref(t: ReleaseTranscription, sha256: str) -> L0Ref:
    return L0Ref(
        source="nifty_index_press_releases",
        logical_date=t.announced,
        filename=t.release,
        sha256=sha256,
        size_bytes=2563857,
        fetched_at=datetime(2026, 10, 5, 16, 24, tzinfo=ZoneInfo("Asia/Kolkata")),
        content_type="application/pdf",
    )


# ── 1. provenance ──────────────────────────────────────────────────────────────────────────────


def test_the_transcription_names_its_l0_object_pages_date_and_transcriber(
    sept2021: ReleaseTranscription,
) -> None:
    assert sept2021.l0_key == f"nifty_index_press_releases/2021/08/{RELEASE}"
    assert len(sept2021.sha256) == 64
    assert sept2021.page_count == 29
    assert sept2021.announced == ANNOUNCED
    assert sept2021.effective == EFFECTIVE and sept2021.effective_page == 1
    assert "September 30, 2021 (close of September 29, 2021)" in sept2021.effective_quote
    assert "agent" in sept2021.transcribed_by
    assert sept2021.transcribed_on == date(2026, 10, 5)
    assert "no OCR" in sept2021.method
    for section in sept2021.sections:
        assert section.pages and all(1 <= p <= 29 for p in section.pages)


def test_the_pinned_sha_is_the_object_in_the_lake(sept2021: ReleaseTranscription) -> None:
    sidecar = _LAKE / "nifty_index_press_releases/2021/08" / f"{RELEASE}.meta.json"
    if not sidecar.is_file():
        pytest.skip("no lake on this host (e.g. CI)")
    assert json.loads(sidecar.read_text())["sha256"] == sept2021.sha256


def test_a_transcription_never_applies_to_other_bytes(sept2021: ReleaseTranscription) -> None:
    parsed = transcription_parse(sept2021, _ref(sept2021, sept2021.sha256))
    assert parsed.release == RELEASE and parsed.unparsed == ()
    with pytest.raises(ParseError, match="re-issued"):
        transcription_parse(sept2021, _ref(sept2021, "0" * 64))


def test_every_event_is_dated_by_the_release_not_by_the_transcription(
    sept2021: ReleaseTranscription,
) -> None:
    parsed = transcription_parse(sept2021, _ref(sept2021, sept2021.sha256))
    assert {e.effective for e in parsed.events} == {EFFECTIVE}
    assert {e.announced for e in parsed.events} == {ANNOUNCED}
    assert {e.l0_key for e in parsed.events} == {sept2021.l0_key}
    # NIFTY 50 is stated unchanged: tracked, with no events.
    assert "nifty50" in parsed.tracked_sections
    assert not [e for e in parsed.events if e.index_slug == "nifty50"]
    per_index = Counter((e.index_slug, e.action) for e in parsed.events)
    assert per_index[("niftynext50", ChangeAction.INCLUDE)] == 5
    assert per_index[("niftysmallcap250", ChangeAction.EXCLUDE)] == 32
    # The section the page cut short yields nothing: half a list applied is a wrong set.
    assert not [e for e in parsed.events if e.index_slug == "nifty500"]


# ── 2. internal consistency ────────────────────────────────────────────────────────────────────


def test_every_complete_tracked_section_balances(sept2021: ReleaseTranscription) -> None:
    slugs = {s.index_slug for s in sept2021.sections} | set(sept2021.unchanged)
    assert slugs == set(TRACKED_INDICES)
    for section in sept2021.sections:
        assert section.balanced or sept2021.truncated(section.index_slug), section.index_slug
    assert [s.index_slug for s in sept2021.sections if not s.balanced] == ["nifty500"]


def test_nifty_100_moves_exactly_as_next_50_does(sept2021: ReleaseTranscription) -> None:
    """NIFTY 100 = NIFTY 50 + Next 50, and NIFTY 50 is unchanged."""
    n100, next50 = _section(sept2021, "nifty100"), _section(sept2021, "niftynext50")
    assert {r.symbol for r in n100.exclude} == {r.symbol for r in next50.exclude}
    assert {r.symbol for r in n100.include} == {r.symbol for r in next50.include}


def test_nifty_200_is_the_net_of_nifty_100_and_midcap_100(sept2021: ReleaseTranscription) -> None:
    out, into = _net(_section(sept2021, "nifty100"), _section(sept2021, "niftymidcap100"))
    n200 = _section(sept2021, "nifty200")
    assert {r.symbol for r in n200.exclude} == out
    assert {r.symbol for r in n200.include} == into


def test_the_text_revision_agrees_with_every_row_it_does_not_change(
    sept2021: ReleaseTranscription, revision: tuple[IndexChangeEvent, ...]
) -> None:
    """ind_prs15092021 restates Midcap 150 / Smallcap 250 after revoking the REIT inclusions.

    Every transcribed row must reappear there except exactly what that revocation moves: EMBASSY,
    MINDSPACE and BIRET not included; so GILLETTE and MOTILALOFS stay in Midcap 150 (and
    MOTILALOFS does not enter Smallcap 250), and HIKAL and HGS enter Smallcap 250 instead.
    A misread symbol anywhere in these 104 rows fails here.
    """
    expected_diff = {
        ("niftymidcap150", ChangeAction.EXCLUDE): ({"GILLETTE", "MOTILALOFS"}, set()),
        ("niftymidcap150", ChangeAction.INCLUDE): ({"EMBASSY", "MINDSPACE"}, set()),
        ("niftysmallcap250", ChangeAction.EXCLUDE): (set(), set()),
        ("niftysmallcap250", ChangeAction.INCLUDE): ({"BIRET", "MOTILALOFS"}, {"HGS", "HIKAL"}),
    }
    for (slug, action), (only_aug, only_sept) in expected_diff.items():
        section = _section(sept2021, slug)
        aug = {
            r.symbol
            for r in (section.exclude if action is ChangeAction.EXCLUDE else section.include)
        }
        sept = _symbols(revision, slug, action)
        assert (aug - sept, sept - aug) == (only_aug, only_sept), (slug, action)


def test_the_truncated_nifty_500_section_is_the_head_of_the_text_revision(
    sept2021: ReleaseTranscription, revision: tuple[IndexChangeEvent, ...]
) -> None:
    """The 19 rows the scan prints are the revision's first 18 plus GILLETTE (kept, see above)."""
    flag = sept2021.truncated("nifty500")
    assert flag is not None and flag.superseded_by == REVISION.name
    printed = [r.symbol for r in _section(sept2021, "nifty500").exclude]
    assert len(printed) == 19 and not _section(sept2021, "nifty500").include
    revised = [
        e.symbol
        for e in revision
        if e.index_slug == "nifty500" and e.action is ChangeAction.EXCLUDE
    ]
    assert len(revised) == 22
    assert [p for p in printed if p != "GILLETTE"] == revised[:18]
    # And the revision's NIFTY 500 is the net of its own components (NIFTY 100 is unchanged).
    out, into = _net(_section(sept2021, "nifty100"))
    for slug in ("niftymidcap150", "niftysmallcap250"):
        for action, bucket in ((ChangeAction.EXCLUDE, -1), (ChangeAction.INCLUDE, 1)):
            for sym in _symbols(revision, slug, action):
                (into if bucket > 0 else out).add(sym)
    both = out & into
    assert set(revised) == out - both
    assert _symbols(revision, "nifty500", ChangeAction.INCLUDE) == into - both


# ── malformed files are refused ────────────────────────────────────────────────────────────────


def _mutated(tmp_path: Path, old: str, new: str) -> Path:
    text = TRANSCRIPTIONS_PATH.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "t.yaml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def test_a_skipped_serial_number_is_refused(tmp_path: Path) -> None:
    path = _mutated(tmp_path, '[2, "Alkem Laboratories Ltd.", ALKEM]', '[3, "Alkem", ALKEM]')
    with pytest.raises(TranscriptionError, match=r"Sr\. No\."):
        load_release_transcriptions(path)


def test_a_truncated_section_must_name_what_superseded_it(tmp_path: Path) -> None:
    path = _mutated(tmp_path, "superseded_by: ind_prs15092021.pdf", "superseded_by: ''")
    with pytest.raises(TranscriptionError, match="superseded_by"):
        load_release_transcriptions(path)


def test_an_unbalanced_section_that_is_not_flagged_is_a_misread(tmp_path: Path) -> None:
    path = _mutated(tmp_path, '          - [5, "United Breweries Ltd.", UBL]\n    ', "    ")
    with pytest.raises(TranscriptionError, match="like for like"):
        load_release_transcriptions(path)


def test_a_section_for_an_untracked_index_is_refused(tmp_path: Path) -> None:
    path = _mutated(tmp_path, "- index: nifty200", "- index: niftymidcap50")
    with pytest.raises(TranscriptionError, match="not a tracked index"):
        load_release_transcriptions(path)


# ── 3. the PIT boundary on the transcribed change ──────────────────────────────────────────────


def _isin(symbol: str) -> str:
    """A stand-in ISIN per symbol — the walk needs identities, not the real ones."""
    return f"INE{zlib.crc32(symbol.encode()) % 10**6:06d}010"


def _next50_history(sept2021: ReleaseTranscription, *, shift_days: int = 0) -> object:
    section = _section(sept2021, "niftynext50")
    parsed = transcription_parse(sept2021, _ref(sept2021, sept2021.sha256))
    events = [e for e in parsed.events if e.index_slug == "niftynext50"]
    if shift_days:
        events = [
            e.model_copy(
                update={"effective": date.fromordinal(e.effective.toordinal() + shift_days)}
            )
            for e in events
        ]
    stayers = [f"INS{n:06d}010" for n in range(45)]
    after = [*stayers, *(_isin(r.symbol) for r in section.include)]
    anchor = ConstituentSnapshot(
        index_slug="niftynext50",
        index_name="niftynext50",
        as_of=date(2021, 10, 22),
        rows=tuple(
            ConstituentRow(isin=i, symbol=f"S{n}", series="EQ", company_name="x", industry="x")
            for n, i in enumerate(after)
        ),
        source="nifty_index_constituents",
    )
    return reconstruct_index(
        "niftynext50",
        anchor=anchor,
        anchor_l0_key="k",
        events=[ResolvedEvent(e, _isin(e.symbol or "")) for e in events],
        coverage_start=date(2021, 6, 1),
        expected_size=50,
    )


def test_the_set_switches_on_the_effective_date_not_on_the_close_of_date(
    sept2021: ReleaseTranscription,
) -> None:
    """Shifting the effective date by a day either way moves the switch and fails here."""
    section = _section(sept2021, "niftynext50")
    outgoing = {_isin(r.symbol) for r in section.exclude}
    incoming = {_isin(r.symbol) for r in section.include}
    history = _next50_history(sept2021)
    eve = history.members_on(date(2021, 9, 29))  # type: ignore[attr-defined]
    day = history.members_on(EFFECTIVE)  # type: ignore[attr-defined]
    assert eve is not None and day is not None and len(eve) == len(day) == 50
    assert outgoing <= eve and not incoming & eve
    assert incoming <= day and not outgoing & day
    early = _next50_history(sept2021, shift_days=-1).members_on(date(2021, 9, 29))  # type: ignore[attr-defined]
    assert early != eve


def test_the_announcement_does_not_leak_the_new_set(sept2021: ReleaseTranscription) -> None:
    """Between announcement and effective date a decision still sees the old set."""
    section = _section(sept2021, "niftynext50")
    incoming = {_isin(r.symbol) for r in section.include}
    history = _next50_history(sept2021)
    for on in (date(2021, 8, 20), ANNOUNCED, date(2021, 9, 1), date(2021, 9, 29)):
        members = history.members_on(on)  # type: ignore[attr-defined]
        assert members is not None and not incoming & members, on
    joined = [i for i in history.intervals if i.isin in incoming]  # type: ignore[attr-defined]
    assert {i.effective_from for i in joined} == {EFFECTIVE}
    assert {i.knowable_from for i in joined} == {ANNOUNCED}
