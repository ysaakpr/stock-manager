"""Re-file the stored non-equity "bonuses" as the schemes of arrangement they are. Offline, one-off.

`parse_terms.names_non_equity_bonus` now files a bonus of preference shares, debentures, NCDs, bonds
or warrants as a SCHEME_OF_ARRANGEMENT with no terms, never as an equity BONUS. Rows parsed before
that are still stored as BONUS — on 2026-10-25 exactly one: TVS Holdings (INE105A01035), NSE,
ex 2023-03-24, `Bonus Ncrps 1:116`, a 116:117 equity factor nobody paid. `reconcile` already
refuses to pair or admit such a row; this replaces it, in one transaction, with the row today's
parser makes from the same text, and runs the CA finalize under the live policy.

What it never does: fetch, re-parse anything but the matching rows' own stored text, or touch a row
whose text it cannot cut to exactly one non-equity bonus. Dry run (the default) opens a READ ONLY
transaction and prints the plan.

    uv run python -m dataplatform.ingest.ca_reparse_non_equity            # dry run
    uv run python -m dataplatform.ingest.ca_reparse_non_equity --apply    # one transaction
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from dataplatform.clock import Clock, SystemClock
from dataplatform.corpactions.factors import PRICE_EVENT_TYPES
from dataplatform.corpactions.parse_terms import (
    names_non_equity_bonus,
    parse_purpose,
    split_compound,
)
from dataplatform.corpactions.taxonomy import ActionType, UnquantifiedTerms
from dataplatform.ingest.corp_actions import (
    CorporateAction,
    load_corporate_actions,
    write_corporate_actions,
)
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = ["Refiling", "apply_refilings", "main", "plan_refilings"]

_LOG = get_logger(__name__)


class RefileError(RuntimeError):
    """A stored row's text did not cut to exactly one non-equity bonus. Nothing was written."""


@dataclass(frozen=True, slots=True)
class Refiling:
    """One stored equity-bonus row, and the scheme-of-arrangement row its own text parses to now."""

    stored: CorporateAction
    refiled: CorporateAction


def plan_refilings(stored: Iterable[CorporateAction]) -> tuple[Refiling, ...]:
    """Every stored split/bonus row whose text is a non-equity bonus, re-parsed from that text.

    Pure. The new row keeps the stored row's ISIN, ex-date, source, dates, references and L0 key —
    only the type and terms are the parser's. Raises `RefileError` on a text that does not cut to
    exactly one non-equity bonus segment, or that the parser does not now file as an unquantified
    scheme of arrangement.
    """
    out: list[Refiling] = []
    for row in stored:
        if row.action_type not in PRICE_EVENT_TYPES or not names_non_equity_bonus(row.raw_text):
            continue
        segments = [s for s in split_compound(row.raw_text) if names_non_equity_bonus(s)]
        if len(segments) != 1:
            raise RefileError(f"{row.isin} {row.ex_date}: {row.raw_text!r} names {len(segments)}")
        parsed = parse_purpose(segments[0], source=row.source).action
        if (
            parsed is None
            or parsed.action_type is not ActionType.SCHEME_OF_ARRANGEMENT
            or not isinstance(parsed.terms, UnquantifiedTerms)
        ):
            raise RefileError(f"{row.isin} {row.ex_date}: {row.raw_text!r} parses to {parsed!r}")
        refiled = row.model_copy(update={"action_type": parsed.action_type, "terms": parsed.terms})
        out.append(Refiling(stored=row, refiled=CorporateAction.model_validate(dict(refiled))))
    return tuple(out)


def apply_refilings(
    conn: Connection, refilings: Sequence[Refiling], *, clock: Clock
) -> tuple[int, int]:
    """Delete each stored row and write its refiling, in the caller's transaction; (deleted,
    inserted). Raises when either count differs from the plan, so the caller rolls back."""
    deleted = 0
    for r in refilings:
        deleted += len(
            conn.execute(
                "DELETE FROM corporate_actions WHERE isin = %s AND ex_date = %s "
                "AND action_type = %s AND source = %s RETURNING id",
                (r.stored.isin, r.stored.ex_date, r.stored.action_type.value, r.stored.source),
            ).fetchall()
        )
    inserted = write_corporate_actions(conn, [r.refiled for r in refilings], clock=clock).inserted
    if (deleted, inserted) != (len(refilings), len(refilings)):
        raise RefileError(
            f"refiled {deleted} deleted / {inserted} inserted, planned {len(refilings)}; roll back"
        )
    _LOG.info(
        "ca_reparse_non_equity.applied", deleted=deleted, inserted=inserted, state="NORMALIZED"
    )
    return deleted, inserted


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan; with `--apply`, refile, finalize, and commit in one transaction."""
    from dataplatform.config import get_settings
    from dataplatform.ingest.ca_refresh import REFRESH_POLICY
    from dataplatform.ingest.corp_actions_backfill import (
        finalize_reconcile_and_recompute,
        reconciled_fingerprints,
    )
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(prog="ingest.ca_reparse_non_equity")
    parser.add_argument("--apply", action="store_true", help="execute and commit")
    args = parser.parse_args(argv)
    configure_logging()
    clock = SystemClock()
    with connection(get_settings()) as conn:
        if not args.apply:
            conn.execute("SET TRANSACTION READ ONLY")
        refilings = plan_refilings(load_corporate_actions(conn))
        print(f"ca_reparse_non_equity: {len(refilings)} row(s) to refile")
        for r in refilings:
            print(
                f"  {r.stored.isin} {r.stored.ex_date} {r.stored.source} "
                f"{r.stored.action_type.value} -> {r.refiled.action_type.value} "
                f"({r.stored.raw_text!r})"
            )
        if not args.apply:
            conn.rollback()
            print("dry run: nothing written (READ ONLY transaction). Re-run with --apply.")
            return 0
        before = reconciled_fingerprints(conn)
        apply_refilings(conn, refilings, clock=clock)
        finalize = finalize_reconcile_and_recompute(
            conn, clock=clock, single_source_policy=REFRESH_POLICY, changed_since=before
        )
        conn.commit()
    print(f"applied; finalize {finalize}")
    print("next: uv run python -m dataplatform.store.l2_fill --rebuild-invalidated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
