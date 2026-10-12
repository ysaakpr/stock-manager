"""D5 (M18.4): reads of the decision record — `decision_journal`, `case_`, the A9 evidence store.

The journal is append-only and permanent (invariant #12): every function here is a SELECT or a
file read, and none opens a connection, reads the clock or writes. They read what `analyst`
persists without importing `analyst` (System 1 never depends on System 2): the journal and the
cases are plain tables, and an evidence bundle is a write-once JSON file under
`<data_root>/evidence/<aa>/<bb>/<sha256>.json` whose name is the sha256 of its bytes.

Everything that anybody wrote — a rationale, a payload, a bundle — is masked before it leaves
here (invariant #13): a journal entry is permanent, so a credential that slipped into one can
never be removed, only kept off every page.

Pagination is keyset on `decision_journal.id`, the journal's append sequence: a page is "the
newest `limit` entries with id below the cursor", so an entry appended while somebody pages
lands on page one and never shifts, repeats or hides an entry on the pages after it — which an
OFFSET would.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from dataplatform.clock import IST
from dataplatform.redaction import mask_and_truncate
from dataplatform.status.models_journal import (
    DECISION_KINDS,
    NO_OP_DECISIONS,
    CaseLineOut,
    CasesOut,
    EvidenceBundleOut,
    EvidenceLineOut,
    EvidenceListOut,
    JournalEntryOut,
    JournalFacetsOut,
    JournalPageOut,
    JournalQuery,
)
from dataplatform.store.db import Connection

__all__ = [
    "EVIDENCE_DIRNAME",
    "EVIDENCE_LIST_LIMIT",
    "PAYLOAD_CHARS",
    "parse_evidence_ref",
    "read_cases",
    "read_evidence_bundle",
    "read_evidence_list",
    "read_journal",
    "read_journal_facets",
]

#: The evidence store's directory under the lake root (mirrors `analyst.journal.EVIDENCE_DIRNAME`).
EVIDENCE_DIRNAME: Final = "evidence"

#: The most bundles the list shows; the total is reported beside it.
EVIDENCE_LIST_LIMIT: Final = 200

#: A payload or bundle is shown whole up to this many characters (after masking).
PAYLOAD_CHARS: Final = 50_000

#: A rationale is prose; bounded like a payload but shorter.
_RATIONALE_CHARS: Final = 8_000

_REF: Final = re.compile(r"sha256:(?P<hex>[0-9a-f]{64})")
_BUNDLE_NAME: Final = re.compile(r"[0-9a-f]{64}\.json")


def _masked(text: str | None, limit: int) -> str | None:
    return None if text is None else mask_and_truncate(text, limit)


def _canonical(value: Any) -> str:
    """Stored JSON as one reads it: sorted keys, indented, non-ASCII kept, Decimals as strings."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)


# ── the journal ────────────────────────────────────────────────────────────────────────────

_JOURNAL_COLUMNS: Final = (
    "id, ts, trading_date, case_id, actor, decision, isin, sleeve, evidence_snapshot_ref, "
    "break_conditions_evaluated, rationale, model, tokens_in, tokens_out, cost_inr, orders_ref, "
    "payload, recorded_at"
)


def _journal_sql(query: JournalQuery) -> tuple[str, list[Any]]:
    """The search as SQL: fixed condition fragments, every value a bound parameter."""
    conditions: list[str] = []
    params: list[Any] = []
    if query.date_from is not None:
        conditions.append("trading_date >= %s")
        params.append(query.date_from)
    if query.date_to is not None:
        conditions.append("trading_date <= %s")
        params.append(query.date_to)
    if query.case_id is not None:
        conditions.append("case_id = %s")
        params.append(query.case_id)
    if query.book is not None:
        conditions.append("payload->>'book' = %s")
        params.append(query.book)
    if query.decision is not None:
        conditions.append("decision = %s")
        params.append(query.decision)
    if query.before is not None:
        conditions.append("id < %s")
        params.append(query.before)
    where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
    # One more than a page, so "is there a next page" needs no second query.
    params.append(query.limit + 1)
    sql = f"SELECT {_JOURNAL_COLUMNS} FROM decision_journal {where}ORDER BY id DESC LIMIT %s"
    return sql, params


def _entry(row: tuple[Any, ...]) -> JournalEntryOut:
    payload = row[16] if isinstance(row[16], Mapping) else {}
    book = payload.get("book")
    return JournalEntryOut(
        id=int(row[0]),
        ts=row[1],
        trading_date=row[2],
        case_id=row[3],
        book=book if isinstance(book, str) else None,
        actor=str(row[4]),
        decision=str(row[5]),
        no_op=str(row[5]) in NO_OP_DECISIONS,
        isin=row[6],
        sleeve=row[7],
        evidence_ref=row[8],
        break_conditions=mask_and_truncate(_canonical(row[9]), PAYLOAD_CHARS),
        rationale=_masked(row[10], _RATIONALE_CHARS),
        model=row[11],
        tokens_in=row[12],
        tokens_out=row[13],
        cost_inr=row[14],
        orders_ref=row[15],
        payload=mask_and_truncate(_canonical(row[16]), PAYLOAD_CHARS),
        recorded_at=row[17],
    )


def read_journal(conn: Connection, query: JournalQuery, *, now: datetime) -> JournalPageOut:
    """One page of `decision_journal` matching `query`, highest id (newest append) first.

    What it does: filters on trading date range, case, M17 stream (`payload.book`) and decision
    kind — no-ops included unless a kind excludes them — and pages by keyset on `id`.
    What it assumes: `query.decision`, if set, is one of `DECISION_KINDS` (the route validates).
    What it never does: write, or return an unmasked rationale or payload.
    """
    sql, params = _journal_sql(query)
    rows = conn.execute(sql, params).fetchall()
    page = [_entry(row) for row in rows[: query.limit]]
    more = len(rows) > query.limit
    return JournalPageOut(
        as_of=now,
        query=query,
        entries=page,
        next_before=page[-1].id if more and page else None,
    )


_CASE_IDS_SQL: Final = "SELECT case_id FROM case_ ORDER BY case_id"
_BOOKS_SQL: Final = (
    "SELECT DISTINCT payload->>'book' FROM decision_journal "
    "WHERE payload->>'book' IS NOT NULL ORDER BY 1"
)


def read_journal_facets(conn: Connection) -> JournalFacetsOut:
    """The search form's choices: every case id, every M17 stream journaled, every kind."""
    return JournalFacetsOut(
        cases=[str(row[0]) for row in conn.execute(_CASE_IDS_SQL).fetchall()],
        books=[str(row[0]) for row in conn.execute(_BOOKS_SQL).fetchall()],
        kinds=list(DECISION_KINDS),
    )


# ── cases ──────────────────────────────────────────────────────────────────────────────────

_CASES_SQL: Final = """
SELECT c.case_id, c.title, c.state, c.funding_mode, c.theme, c.created_at, c.updated_at,
       coalesce(j.entries, 0), j.last_trading_date
FROM case_ c
LEFT JOIN (
    SELECT case_id, count(*) AS entries, max(trading_date) AS last_trading_date
    FROM decision_journal
    WHERE case_id IS NOT NULL
    GROUP BY case_id
) j ON j.case_id = c.case_id
ORDER BY c.case_id
"""


def read_cases(conn: Connection, *, now: datetime) -> CasesOut:
    """Every `case_` row with its §5.1 lifecycle stage and its journal entry count."""
    return CasesOut(
        as_of=now,
        cases=[
            CaseLineOut(
                case_id=str(row[0]),
                title=mask_and_truncate(str(row[1]), 400),
                state=str(row[2]),
                funding_mode=str(row[3]),
                theme=_masked(row[4], 400),
                created_at=row[5],
                updated_at=row[6],
                entries=int(row[7]),
                last_trading_date=row[8],
            )
            for row in conn.execute(_CASES_SQL).fetchall()
        ],
    )


# ── the A9 evidence store ──────────────────────────────────────────────────────────────────

_EVIDENCE_REFS_SQL: Final = """
SELECT evidence_snapshot_ref, count(*), max(ts)
FROM decision_journal
WHERE evidence_snapshot_ref IS NOT NULL
GROUP BY evidence_snapshot_ref
"""

_CITED_BY_SQL: Final = """
SELECT id FROM decision_journal WHERE evidence_snapshot_ref = %s ORDER BY id DESC LIMIT 200
"""


def parse_evidence_ref(text: str) -> str | None:
    """The hex digest of a well-formed ``sha256:<64 hex>`` reference, else None."""
    match = _REF.fullmatch(text.strip())
    return None if match is None else match["hex"]


def _bundle_path(root: Path, digest: str) -> Path:
    return root / digest[:2] / digest[2:4] / f"{digest}.json"


def _written_at(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=IST)


def read_evidence_list(
    conn: Connection, data_root: Path, *, now: datetime, limit: int = EVIDENCE_LIST_LIMIT
) -> EvidenceListOut:
    """The stored bundles, newest file first, each with how many journal entries cite it.

    What it assumes: the store's layout (`<aa>/<bb>/<sha256>.json`); a file not named that way
    is not a bundle and is not listed.
    What it never does: read a bundle's content (opening one is `read_evidence_bundle`), or
    report an unmounted store as an empty one — `root_visible` says which it is.
    """
    root = data_root / EVIDENCE_DIRNAME
    if not root.is_dir():
        return EvidenceListOut(
            as_of=now, root=str(root), root_visible=False, total=0, limit=limit, bundles=[]
        )
    cited = {
        str(row[0]): (int(row[1]), row[2]) for row in conn.execute(_EVIDENCE_REFS_SQL).fetchall()
    }
    files = [path for path in root.glob("*/*/*.json") if _BUNDLE_NAME.fullmatch(path.name)]
    dated = sorted(((_written_at(p), p) for p in files), key=lambda x: (x[0], x[1].name))
    dated.reverse()
    bundles = []
    for written_at, path in dated[:limit]:
        ref = f"sha256:{path.stem}"
        entries, last = cited.get(ref, (0, None))
        bundles.append(
            EvidenceLineOut(
                ref=ref,
                bytes=path.stat().st_size,
                written_at=written_at,
                entries=entries,
                last_referenced_at=last,
            )
        )
    return EvidenceListOut(
        as_of=now, root=str(root), root_visible=True, total=len(files), limit=limit, bundles=bundles
    )


def read_evidence_bundle(
    conn: Connection, data_root: Path, digest: str, *, now: datetime
) -> EvidenceBundleOut | None:
    """One bundle by its hex digest, re-hashed against its name; None when no file holds it.

    A bundle whose bytes no longer hash to its name is still shown — masked, and flagged
    `verified=False` — because hiding a tampered record is worse than showing it marked.
    What it assumes: `digest` is 64 lower-case hex characters (`parse_evidence_ref`).
    """
    path = _bundle_path(data_root / EVIDENCE_DIRNAME, digest)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    try:
        content = _canonical(json.loads(text))
    except json.JSONDecodeError:
        content = text
    ref = f"sha256:{digest}"
    return EvidenceBundleOut(
        as_of=now,
        ref=ref,
        bytes=len(raw),
        written_at=_written_at(path),
        verified=hashlib.sha256(raw).hexdigest() == digest,
        content=mask_and_truncate(content, PAYLOAD_CHARS),
        referenced_by=[int(row[0]) for row in conn.execute(_CITED_BY_SQL, (ref,)).fetchall()],
    )
