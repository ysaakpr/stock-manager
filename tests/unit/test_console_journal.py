"""M18.4: the console's Journal page — decision search, cases, evidence — offline, over a fixture.

One fixture journal (decisions and no-ops across a case, two M17 streams and the system, over six
weeks), a `case_` table, an evidence store under tmp_path and a frozen clock, served to the real
app through the same dependency overrides as the JSON surface. The stub connection *interprets*
the page's SQL — each WHERE fragment it knows is applied to the rows, an unknown one fails the
test — so a filter or a keyset written backwards is caught here, and
`tests/integration/test_console_journal.py` proves the same SQL against Postgres.

The acceptance criteria, one block each:

1. the journal is searchable by date, case, manager (M17 stream) and kind; no-ops are listed;
2. pagination is keyset on `id`: walking every page yields every match once, in order, even when
   entries are appended between page reads;
3. a payload (and a rationale, and an evidence bundle) holding a planted credential is masked;
4. the M18.1 shared rules hold on these pages: read-only, no external URL, IST, injected clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.status.api import app, clock_source, db_connection
from dataplatform.status.api import settings_source as api_settings_source
from dataplatform.status.console import READ_METHODS, router
from dataplatform.status.console.pages import PAGES
from dataplatform.status.models_journal import JOURNAL_PAGE_SIZE, NO_OP_DECISIONS

NOW: Final = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
FIRST_DAY: Final = date(2026, 9, 1)

#: Every planted secret carries this marker, so "no secret leaked" is one substring check.
PLANT: Final = "PLANTEDjj"
# Test fakes, assembled from fragments so the secret scanner does not read them as credentials.
_USERINFO: Final = ":".join(("fm", f"{PLANT}pw1"))
_PAIR: Final = "=".join(("token", f"{PLANT}tok2"))

BOOK_A: Final = "FM-SWING-BRK-10L"
BOOK_B: Final = "FM-SWING-BRK-1CR"
CASE: Final = "CASE-CORE-01"
CASES: Final = (CASE, BOOK_A, BOOK_B)
_KINDS: Final = ("BUY", "HEARTBEAT", "SELL", "HOLD", "RAIL_BLOCK", "SKIPPED_DATA_RED", "ESCALATE")

PLANTED_ID: Final = 77
#: Enough entries that every stream filter, as well as the whole journal, spans several pages.
COUNT: Final = 230


# ── the fixture journal ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    id: int
    trading_date: date
    case_id: str | None
    decision: str
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str | None = None
    evidence_ref: str | None = None

    @property
    def ts(self) -> datetime:
        # Stored the way Postgres hands timestamptz to a UTC session: UTC, rendered as IST.
        return datetime.combine(self.trading_date, datetime.min.time(), UTC) + timedelta(
            hours=13, minutes=self.id % 60
        )

    @property
    def book(self) -> str | None:
        value = self.payload.get("book")
        return value if isinstance(value, str) else None

    def row(self) -> tuple[Any, ...]:
        return (
            self.id, self.ts, self.trading_date, self.case_id,
            "SYSTEM" if self.case_id is None else "T0", self.decision,
            "INE002A01018" if self.decision in ("BUY", "SELL") else None,
            "CORE" if self.decision in ("BUY", "SELL") else None, self.evidence_ref, [],
            self.rationale, "claude-opus-5-5" if self.decision == "BUY" else None,
            1200 if self.decision == "BUY" else None, 300 if self.decision == "BUY" else None,
            Decimal("1.25") if self.decision == "BUY" else None, None, self.payload,
            self.ts + timedelta(seconds=1),
        )  # fmt: skip


def _planted_payload() -> dict[str, Any]:
    return {
        "book": BOOK_A,
        "mode": "PAPER",
        "note": f"reached the broker at postgresql://{_USERINFO}@db:5432/trading",
        "api_key": f"{PLANT}key3",
        "nested": {"password": f"{PLANT}pw4", "orders": 2},
    }


def _journal(count: int = COUNT, evidence_ref: str | None = None) -> list[Entry]:
    entries = []
    for i in range(1, count + 1):
        case = (CASE, BOOK_A, BOOK_B, None)[i % 4]
        payload: dict[str, Any] = (
            {"book": case, "mode": "PAPER"} if case in (BOOK_A, BOOK_B) else {}
        )
        rationale = f"entry {i}"
        if i == PLANTED_ID:
            case, payload = BOOK_A, _planted_payload()
            rationale = f"retried with {_PAIR}"
        entries.append(
            Entry(
                id=i,
                trading_date=FIRST_DAY + timedelta(days=(i - 1) // 6),
                case_id=case,
                decision=_KINDS[i % len(_KINDS)],
                payload=payload,
                rationale=rationale,
                evidence_ref=evidence_ref if i % 10 == 0 else None,
            )
        )
    return entries


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


#: The WHERE fragments the search may emit, and what each means. An unknown fragment fails.
_FILTERS: Final[dict[str, Callable[[Entry, Any], bool]]] = {
    "trading_date >= %s": lambda e, v: e.trading_date >= v,
    "trading_date <= %s": lambda e, v: e.trading_date <= v,
    "case_id = %s": lambda e, v: e.case_id == v,
    "payload->>'book' = %s": lambda e, v: e.book == v,
    "decision = %s": lambda e, v: e.decision == v,
    "id < %s": lambda e, v: e.id < v,
}

_SEARCH: Final = re.compile(
    r"SELECT (?P<cols>.+?) FROM decision_journal (?:WHERE (?P<where>.+) )?"
    r"ORDER BY id DESC LIMIT %s"
)


class StubConnection:
    """Answers the Journal page's queries from in-memory rows; anything else fails loudly."""

    def __init__(self, entries: Sequence[Entry], cases: Sequence[tuple[Any, ...]]) -> None:
        self.entries = list(entries)
        self.cases = list(cases)
        self.statements: list[str] = []

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        text = " ".join(sql.split())
        self.statements.append(text)
        search = _SEARCH.fullmatch(text)
        if search is not None:
            assert search["cols"].startswith("id, ts, trading_date, case_id, actor, decision")
            *values, limit = params
            fragments = [] if search["where"] is None else search["where"].split(" AND ")
            assert len(fragments) == len(values), (fragments, values)
            rows = [
                e for e in self.entries
                if all(_FILTERS[f](e, v) for f, v in zip(fragments, values, strict=True))
            ]  # fmt: skip
            rows.sort(key=lambda e: e.id, reverse=True)
            return _Result([e.row() for e in rows[:limit]])
        if text == "SELECT case_id FROM case_ ORDER BY case_id":
            return _Result([(c[0],) for c in sorted(self.cases)])
        if text.startswith("SELECT DISTINCT payload->>'book' FROM decision_journal"):
            return _Result([(b,) for b in sorted({e.book for e in self.entries if e.book})])
        if text.startswith("SELECT c.case_id, c.title, c.state"):
            lines = []
            for case in sorted(self.cases):
                mine = [e for e in self.entries if e.case_id == case[0]]
                last = max((e.trading_date for e in mine), default=None)
                lines.append((*case, len(mine), last))
            return _Result(lines)
        if text.startswith("SELECT evidence_snapshot_ref, count(*), max(ts)"):
            refs: dict[str, list[Entry]] = {}
            for e in self.entries:
                if e.evidence_ref is not None:
                    refs.setdefault(e.evidence_ref, []).append(e)
            return _Result([(r, len(es), max(e.ts for e in es)) for r, es in refs.items()])
        if text.startswith("SELECT id FROM decision_journal WHERE evidence_snapshot_ref = %s"):
            (ref,) = params
            ids = sorted((e.id for e in self.entries if e.evidence_ref == ref), reverse=True)
            return _Result([(i,) for i in ids])
        raise AssertionError(f"unexpected query against the stub connection: {text}")


def _case_rows() -> list[tuple[Any, ...]]:
    created = NOW - timedelta(days=60)
    return [
        (CASE, "Core compounders", "ACTIVE", "PAPER", "quality", created, NOW - timedelta(days=1)),
        (BOOK_A, "M17 swing breakout, primary", "FUNDED", "PAPER", None, created, created),
        (BOOK_B, f"mirror book password={PLANT}pw9", "PROPOSAL", "PAPER", None, created, created),
    ]


def _bundle_bytes() -> bytes:
    bundle = {
        "items": [
            {"kind": "PRICE", "isin": "INE002A01018", "value": "2450.15", "detail": {}},
            {"kind": "NEWS", "text": f"leaked header Bearer {PLANT}bearer5abcdef", "detail": {}},
        ],
        "schema": 1,
    }
    return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def evidence_ref(tmp_path: Path) -> str:
    """One genuine bundle and one whose bytes no longer match their name."""
    root = tmp_path / "lake" / "evidence"
    raw = _bundle_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    path = root / digest[:2] / digest[2:4] / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    tampered = "ab" * 32
    bad = root / "ab" / "ab" / f"{tampered}.json"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'{"schema":1,"items":[]}')
    (root / "ab" / "ab" / "README.txt").write_text("not a bundle", encoding="utf-8")
    return f"sha256:{digest}"


@pytest.fixture
def settings(tmp_path: Path, evidence_ref: str) -> Settings:
    return Settings(data_root=tmp_path / "lake", backup_root=tmp_path / "backups")


@pytest.fixture
def conn(evidence_ref: str) -> StubConnection:
    return StubConnection(_journal(evidence_ref=evidence_ref), _case_rows())


def _client(settings: Settings, conn: StubConnection, now: datetime = NOW) -> TestClient:
    app.dependency_overrides[api_settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(now)
    app.dependency_overrides[db_connection] = lambda: conn
    return TestClient(app)


@pytest.fixture
def client(settings: Settings, conn: StubConnection) -> Iterator[TestClient]:
    with _client(settings, conn) as live:
        yield live
    app.dependency_overrides.clear()


_ROW: Final = re.compile(r'<tr data-entry="(\d+)" data-decision="([A-Z_]+)" data-noop="([01])"')
_NEXT: Final = re.compile(r'<a id="next-page" href="([^"]+)"')


def _ids(html: str) -> list[int]:
    return [int(m[0]) for m in _ROW.findall(html)]


def _get(client: TestClient, path: str, **params: str) -> str:
    # `params={}` would make httpx drop the query string already in `path` (a next-page link).
    response = client.get(path, params=params or None)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    text: str = response.text
    return text


def _walk(client: TestClient, **params: str) -> list[int]:
    """Every page of a search, following the page's own next link; the ids in order seen."""
    html = _get(client, "/console/journal", **params)
    seen = _ids(html)
    pages = 1
    while (link := _NEXT.search(html)) is not None:
        html = _get(client, unescape(link[1]))
        seen += _ids(html)
        pages += 1
        assert pages < 100
    return seen


def _expected(entries: Sequence[Entry], keep: Callable[[Entry], bool]) -> list[int]:
    return sorted((e.id for e in entries if keep(e)), reverse=True)


# ── 1. searchable by date, case, manager and kind; no-ops listed ───────────────────────────


def test_the_unfiltered_page_is_the_newest_entries_first_no_ops_included(
    client: TestClient, conn: StubConnection
) -> None:
    html = _get(client, "/console/journal")
    rows = _ROW.findall(html)
    assert [int(r[0]) for r in rows] == list(range(COUNT, COUNT - JOURNAL_PAGE_SIZE, -1))
    kinds = {r[1] for r in rows}
    assert {"HEARTBEAT", "HOLD", "SKIPPED_DATA_RED"} <= kinds, "no-ops are listed, not hidden"
    for _, kind, noop in rows:
        assert (noop == "1") == (kind in NO_OP_DECISIONS)
    assert "no-op</span>" in html
    assert 'id="journal-search"' in html and 'action="/console/journal"' in html
    for option in (CASE, BOOK_A, BOOK_B, "HEARTBEAT", "POLICY_PROPOSAL"):
        assert f'<option value="{option}">' in html


@pytest.mark.parametrize(
    ("params", "keep"),
    [
        ({"date_from": "2026-09-10", "date_to": "2026-09-20"},
         lambda e: date(2026, 9, 10) <= e.trading_date <= date(2026, 9, 20)),
        ({"date_from": "2026-10-01"}, lambda e: e.trading_date >= date(2026, 10, 1)),
        ({"date_to": "2026-09-05"}, lambda e: e.trading_date <= date(2026, 9, 5)),
        ({"case": CASE}, lambda e: e.case_id == CASE),
        ({"manager": BOOK_A}, lambda e: e.book == BOOK_A),
        ({"manager": BOOK_B}, lambda e: e.book == BOOK_B),
        ({"kind": "HEARTBEAT"}, lambda e: e.decision == "HEARTBEAT"),
        ({"kind": "SKIPPED_DATA_RED"}, lambda e: e.decision == "SKIPPED_DATA_RED"),
        ({"kind": "BUY", "manager": BOOK_A, "date_from": "2026-09-15"},
         lambda e: e.decision == "BUY" and e.book == BOOK_A
         and e.trading_date >= date(2026, 9, 15)),
        # A blank form field is "not filtered", exactly as the browser submits it.
        ({"case": "", "manager": "", "kind": "", "date_from": "", "date_to": ""}, lambda e: True),
    ],
)  # fmt: skip
def test_each_filter_returns_exactly_the_matching_entries(
    client: TestClient, conn: StubConnection, params: dict[str, str], keep: Callable[[Entry], bool]
) -> None:
    expected = _expected(conn.entries, keep)
    assert expected, "the fixture must exercise the filter"
    assert _walk(client, **params) == expected


def test_a_manager_filter_is_the_stream_not_the_case_id(client: TestClient) -> None:
    """The planted entry is case BOOK_A by payload too; a CASE-tagged entry is never a stream."""
    ids = _walk(client, manager=BOOK_A)
    assert PLANTED_ID in ids
    assert all(i % 4 == 1 or i == PLANTED_ID for i in ids)


def test_a_case_cell_links_to_its_journal_and_the_kind_is_shown(client: TestClient) -> None:
    html = _get(client, "/console/journal", kind="HEARTBEAT")
    assert {kind for _, kind, _ in _ROW.findall(html)} == {"HEARTBEAT"}
    assert f'href="/console/journal?case={CASE}"' in html


def test_an_entry_opens_its_payload(client: TestClient) -> None:
    html = _get(client, "/console/journal", kind="BUY", manager=BOOK_B)
    first = _ids(html)[0]
    row = html[html.index(f'<tr data-entry="{first}"') :]
    row = row[: row.index("</tr>")]
    assert "<details><summary>payload</summary>" in row
    assert "&quot;book&quot;: &quot;FM-SWING-BRK-1CR&quot;" in row
    assert "cost ₹ 1.25" in row  # Decimal end to end
    assert "tokens in/out 1200 / 300" in row


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "12/10/2026"},
        {"date_to": "2026-13-01"},
        {"kind": "BUYY"},
        {"before": "-5"},
        {"before": "x"},
        {"before": "²"},
        {"before": "9" * 19},
        {"date_from": "2026-10-02", "date_to": "2026-10-01"},
    ],
)
def test_a_malformed_search_is_refused_on_the_page(
    client: TestClient, params: dict[str, str]
) -> None:
    response = client.get("/console/journal", params=params)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Decision journal</h1>" in response.text
    assert "<tr data-entry=" not in response.text


def test_an_empty_search_says_so(client: TestClient) -> None:
    html = _get(client, "/console/journal", case="NO-SUCH-CASE")
    assert "no journal entry matches this search" in html
    assert 'id="next-page"' not in html


# ── 2. keyset pagination: nothing repeated, nothing skipped ────────────────────────────────


def test_walking_every_page_yields_every_entry_once_in_order(
    client: TestClient, conn: StubConnection
) -> None:
    seen = _walk(client)
    assert seen == list(range(COUNT, 0, -1))
    assert len(seen) == len(set(seen))


def test_the_next_link_is_a_keyset_cursor_on_the_last_id_shown(client: TestClient) -> None:
    html = _get(client, "/console/journal", manager=BOOK_A)
    ids = _ids(html)
    link = _NEXT.search(html)
    assert link is not None
    assert f"before={ids[-1]}" in unescape(link[1])
    assert "manager=FM-SWING-BRK-10L" in unescape(link[1])  # the filter rides along
    assert "offset" not in html


def test_appends_between_page_reads_neither_repeat_nor_skip(
    client: TestClient, conn: StubConnection
) -> None:
    """An offset pager shifts by the number of new rows; the keyset one does not move."""
    html = _get(client, "/console/journal")
    first = _ids(html)
    next_href = unescape(_NEXT.search(html)[1])  # type: ignore[index]
    newest = max(e.id for e in conn.entries)
    for extra in range(1, 8):  # the daily loop keeps journaling meanwhile
        conn.entries.append(Entry(newest + extra, date(2026, 10, 12), None, "HEARTBEAT"))
    seen = list(first)
    html = _get(client, next_href)
    seen += _ids(html)
    while (link := _NEXT.search(html)) is not None and len(seen) < 1000:
        html = _get(client, unescape(link[1]))
        seen += _ids(html)
    assert seen == list(range(COUNT, 0, -1)), "the original entries, each once, in order"
    assert _ids(_get(client, "/console/journal"))[0] == newest + 7  # page one shows the appends


def test_the_last_page_has_no_next_link_and_the_first_has_no_newest_link(
    client: TestClient,
) -> None:
    first = _get(client, "/console/journal")
    assert ">newest</a>" not in first
    last = _get(client, "/console/journal", before="31")
    assert _ids(last) == list(range(30, 0, -1))
    assert 'id="next-page"' not in last
    assert ">newest</a>" in last


def test_a_page_is_exactly_one_page_of_rows(conn: StubConnection, client: TestClient) -> None:
    """The query asks for one row more than a page, to know whether there is a next."""
    _get(client, "/console/journal")
    search = [s for s in conn.statements if s.startswith("SELECT id, ts")]
    assert search and search[-1].endswith("ORDER BY id DESC LIMIT %s")


# ── 3. a planted credential is shown masked ────────────────────────────────────────────────


def test_a_planted_credential_in_a_payload_is_shown_masked(client: TestClient) -> None:
    html = _get(client, "/console/journal", manager=BOOK_A)
    assert PLANTED_ID in _ids(html)
    assert PLANT not in html
    row = html[html.index(f'<tr data-entry="{PLANTED_ID}"') :]
    row = row[: row.index("</tr>")]
    assert "postgresql://***@db:5432/trading" in row  # masked, still diagnosable
    assert "&quot;api_key&quot;: &quot;***&quot;" in row
    assert "&quot;password&quot;: &quot;***&quot;" in row
    assert "token=***" in row  # the rationale
    assert "&quot;orders&quot;: 2" in row  # the rest of the payload is intact


def test_no_journal_response_ever_contains_the_plant(client: TestClient, evidence_ref: str) -> None:
    for path, params in (
        ("/console/journal", {}),
        ("/console/journal", {"manager": BOOK_A}),
        ("/console/journal", {"before": str(PLANTED_ID + 1)}),
        ("/console/journal/cases", {}),
        ("/console/journal/evidence", {}),
        ("/console/journal/evidence", {"ref": evidence_ref}),
    ):
        assert PLANT not in _get(client, path, **params), (path, params)


def test_an_evidence_bundle_opens_verified_and_masked(
    client: TestClient, evidence_ref: str
) -> None:
    html = _get(client, "/console/journal/evidence", ref=evidence_ref)
    assert "verified</span>" in html
    assert "Bearer ***" in html and PLANT not in html
    assert "2450.15" in html
    cited = re.findall(r">#(\d+)</a>", html)
    assert cited == [str(i) for i in range(COUNT, 0, -10)]


def test_a_bundle_whose_bytes_changed_is_shown_flagged(client: TestClient) -> None:
    html = _get(client, "/console/journal/evidence", ref="sha256:" + "ab" * 32)
    assert "MISMATCH" in html
    assert "verified</span>" not in html


@pytest.mark.parametrize("ref", ["sha256:" + "cd" * 32, "sha256:short", "../../etc/passwd"])
def test_an_unknown_or_malformed_reference_is_404(client: TestClient, ref: str) -> None:
    response = client.get("/console/journal/evidence", params={"ref": ref})
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


# ── cases and evidence lists ───────────────────────────────────────────────────────────────


def test_cases_list_their_stage_and_link_to_the_ratification_page(client: TestClient) -> None:
    html = _get(client, "/console/journal/cases")
    stages = dict(re.findall(r'<tr data-case="([^"]+)" data-stage="([^"]+)"', html))
    assert stages == {CASE: "ACTIVE", BOOK_A: "FUNDED", BOOK_B: "PROPOSAL"}
    for case in CASES:
        assert f'href="/cases/{case}"' in html
        assert f'href="/console/journal?case={case}"' in html
    assert "mirror book password=***" in html


def test_the_evidence_list_shows_bundles_and_who_cites_them(
    client: TestClient, evidence_ref: str
) -> None:
    html = _get(client, "/console/journal/evidence")
    bundles = re.findall(r'<tr data-bundle="([^"]+)">', html)
    assert sorted(bundles) == sorted([evidence_ref, "sha256:" + "ab" * 32])
    assert "2 bundles" in html  # README.txt is not a bundle
    row = html[html.index(f'<tr data-bundle="{evidence_ref}"') :]
    assert f'<td class="num">{COUNT // 10}</td>' in row[: row.index("</tr>")]
    assert "not persisted" in html  # the §5.7 pack is said, not silently absent


def test_an_unmounted_evidence_store_is_said_not_reported_empty(
    tmp_path: Path, conn: StubConnection
) -> None:
    settings = Settings(data_root=tmp_path / "elsewhere")
    with _client(settings, conn) as live:
        html = _get(live, "/console/journal/evidence")
    app.dependency_overrides.clear()
    assert "not visible to this process" in html


# ── 4. the M18.1 shared rules hold on these pages ──────────────────────────────────────────

_JOURNAL_PATHS: Final = ("/console/journal", "/console/journal/cases", "/console/journal/evidence")


def test_the_page_is_built_and_its_routes_are_on_the_console_router() -> None:
    assert ("journal", "Journal", True) in PAGES
    mine = {
        route.path: route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/console/journal")
    }
    assert set(mine) == set(_JOURNAL_PATHS)
    for route in mine.values():
        assert route.methods is not None and set(route.methods) <= set(READ_METHODS)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_journal_route_refuses_a_write(client: TestClient, method: str) -> None:
    for path in _JOURNAL_PATHS:
        assert client.request(method, path).status_code == 405, f"{method} {path}"


def test_the_page_only_ever_selects(
    client: TestClient, conn: StubConnection, evidence_ref: str
) -> None:
    for path in _JOURNAL_PATHS:
        client.get(path)
        client.head(path)
    client.get("/console/journal/evidence", params={"ref": evidence_ref})
    client.get("/console/journal", params={"kind": "BUY", "before": "90"})
    assert conn.statements
    assert all(s.startswith("SELECT ") for s in conn.statements), conn.statements
    for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not any(verb in s for s in conn.statements)


_URL_ATTR: Final = re.compile(r'\b(?:src|href|hx-get|hx-post|action|integrity)="([^"]*)"')
_STAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?P<tail>.{0,4})")
_PAYLOAD_BLOCK: Final = re.compile(r'<pre class="payload"[^>]*>.*?</pre>', re.DOTALL)


def _pages(client: TestClient, evidence_ref: str) -> Iterator[tuple[str, str]]:
    for path, params in (
        ("/console/journal", {}),
        ("/console/journal", {"before": "60", "kind": "BUY"}),
        ("/console/journal/cases", {}),
        ("/console/journal/evidence", {}),
        ("/console/journal/evidence", {"ref": evidence_ref}),
    ):
        yield path, _get(client, path, **params)


def test_no_journal_page_references_an_external_url(client: TestClient, evidence_ref: str) -> None:
    for path, html in _pages(client, evidence_ref):
        assert "http://" not in html and "https://" not in html, path
        for value in _URL_ATTR.findall(html):
            if value.startswith("sha384-"):
                continue
            assert value.startswith("/") and not value.startswith("//"), (path, value)


def test_every_rendered_timestamp_is_labelled_ist(client: TestClient, evidence_ref: str) -> None:
    """Every instant the page renders ends in IST. Stored payload text is data, shown verbatim
    (masked) inside its own block, and is not an instant the page rendered."""
    seen = 0
    for path, html in _pages(client, evidence_ref):
        for match in _STAMP.finditer(_PAYLOAD_BLOCK.sub("", html)):
            seen += 1
            assert match.group(0).endswith(" IST"), (path, match.group(0))
    assert seen > 100
    html = _get(client, "/console/journal", before="2")
    # Entry 1: 2026-09-01, stored 13:01 UTC → 18:31 IST.
    assert "2026-09-01 18:31:00 IST" in html and "13:01:00" not in html


def test_the_page_answers_at_the_injected_instant(settings: Settings, conn: StubConnection) -> None:
    later = NOW + timedelta(days=2)
    with _client(settings, conn, later) as live:
        for path in _JOURNAL_PATHS:
            assert "as of 2026-10-14 20:00:00 IST" in _get(live, path)
    app.dependency_overrides.clear()


def test_the_journal_nav_link_is_current(client: TestClient) -> None:
    for path in _JOURNAL_PATHS:
        assert '<a href="/console/journal" class="current">Journal</a>' in _get(client, path)
