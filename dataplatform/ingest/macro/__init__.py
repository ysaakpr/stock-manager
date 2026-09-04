"""D1: macro and market-state series — the point-in-time economic backdrop.

Two things live here and they are deliberately different in kind:

* **`models`** — `MacroFact`, the one shape every macro series lands in, and the canonical
  `series_id` taxonomy. A fact carries the period it *describes* and the date it became
  *knowable*, and those are not the same date for anything published on a lag (April CPI is not
  knowable in April). Everything downstream reads through the second one.
* **`index_valuation`** — the first producer: NSE's daily close-all snapshot, which publishes a
  closing value, P/E, P/B and dividend yield for every NIFTY index and reaches back to late 2012.

Nothing here computes a regime, a label or a tag. Those are derived (L2) and rebuildable; this
layer stores only the published primitive, because a number you cannot reconstruct from its inputs
is un-auditable (the argument `ops/gates/M10-data-gap-plan.md` already makes about filer-stated
ratios, applied to macro).
"""

from dataplatform.ingest.macro.index_valuation import (
    INDEX_ALIASES_PATH,
    IndexAliasTable,
    canonical_index,
    load_index_aliases,
    parse_index_valuation,
)
from dataplatform.ingest.macro.models import (
    Frequency,
    MacroFact,
    MacroRelease,
    Unit,
    series_id,
)

__all__ = [
    "INDEX_ALIASES_PATH",
    "Frequency",
    "IndexAliasTable",
    "MacroFact",
    "MacroRelease",
    "Unit",
    "canonical_index",
    "load_index_aliases",
    "parse_index_valuation",
    "series_id",
]
