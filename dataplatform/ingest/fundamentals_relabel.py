"""Where a relabelled XBRL filing's provenance is written: `fundamentals_period_relabel` (0024).

The runner (`FundamentalsBackfillRunner`) stores a class-A filing under the quarter the owner's
check (`xbrl.q4_relabel`) allows only when it has somewhere to record that it did; this module is
that somewhere. The row is written on the runner's connection and committed with the filing's own
`PUBLISHED` checkpoint, so a relabelled filing and its provenance are never apart.

What it never does: commit on its own, or write anything but the record it is handed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final

from dataplatform.clock import Clock
from dataplatform.ingest.xbrl.q4_relabel import RelabelRecord
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = ["RecordRelabel", "recorder"]

_LOG = get_logger(__name__)

#: Records one relabelled filing's provenance, uncommitted.
RecordRelabel = Callable[[RelabelRecord], None]

_UPSERT_SQL: Final = """
INSERT INTO fundamentals_period_relabel
    (source, unit, logical_date, filing_id, isin, nature, stated_start, stated_end, stored_start,
     stored_end, rule, check_result, l0_key, recorded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
ON CONFLICT (source, unit, logical_date) DO UPDATE SET
    filing_id = EXCLUDED.filing_id, isin = EXCLUDED.isin, nature = EXCLUDED.nature,
    stated_start = EXCLUDED.stated_start, stated_end = EXCLUDED.stated_end,
    stored_start = EXCLUDED.stored_start, stored_end = EXCLUDED.stored_end,
    rule = EXCLUDED.rule, check_result = EXCLUDED.check_result, l0_key = EXCLUDED.l0_key,
    recorded_at = EXCLUDED.recorded_at
"""


def recorder(conn: Connection, clock: Clock) -> RecordRelabel:
    """A `RecordRelabel` writing to `fundamentals_period_relabel` on `conn`, left uncommitted.

    What it assumes: the caller commits — the runner does, as the filing's checkpoint.
    """

    def record(row: RelabelRecord) -> None:
        conn.execute(
            _UPSERT_SQL,
            (
                row.source,
                row.unit,
                row.logical_date,
                row.filing_id,
                row.isin,
                row.nature,
                row.stated_start,
                row.stated_end,
                row.stored_start,
                row.stored_end,
                row.rule,
                json.dumps(dict(row.check_result), sort_keys=True),
                row.l0_key,
                clock.now(),
            ),
        )
        _LOG.info(
            "fundamentals_relabel.recorded",
            source=row.source,
            unit=row.unit,
            date=row.logical_date.isoformat(),
            isin=row.isin,
            nature=row.nature,
            stated=f"{row.stated_start.isoformat()}..{row.stated_end.isoformat()}",
            stored=f"{row.stored_start.isoformat()}..{row.stored_end.isoformat()}",
            rule=row.rule,
            state="NORMALIZED",
        )

    return record
