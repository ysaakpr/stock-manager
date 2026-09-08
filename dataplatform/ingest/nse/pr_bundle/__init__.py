"""D1: the NSE daily report bundle (`PR<DDMMYY>.zip`) — W2.

One zip per session on the archive host from 2010-01-04, carrying ~14-25 member reports. This
package opens the bundle (`bundle.py`), and parses the three members W2 targets:

* `bc.py`   — corporate actions **dated by the file's own publication date**. The reason the
              source is in the platform at all: it is the only surface that makes invariant #7
              non-vacuous for history.
* `ix.py`   — index membership with weightage. Intermittent, 2010 only; a validation asset rather
              than a reconstructable series, and the docstring says so with the measurements.
* `mcap.py` — daily issue size, market cap and last-trade-date. ~2024-07 onward.

Every other member is registered by name in `MemberKind` and parsed by nothing yet.

**Nothing in this package reads a clock.** A corporate action's knowable date comes from the
bundle it was published in, never from ingest time — see `PrBundle.publication_date`.

**Nothing in this package writes to `corporate_actions`.** Promoting these rows against the
47,887 rows already there is a separate, separately-reviewed task; getting it wrong corrupts the
adjustment chain.
"""

from dataplatform.ingest.nse.pr_bundle.bc import BC_COLUMNS, BcRow, parse_bc, parse_bc_bundle
from dataplatform.ingest.nse.pr_bundle.bundle import (
    ARCHIVE_START,
    LOWERCASE_ERA_START,
    MCAP_ERA_START,
    PR_BUNDLE_SOURCE_ID,
    URL_TEMPLATE,
    BundleMember,
    MemberKind,
    PrBundle,
    url_for,
)
from dataplatform.ingest.nse.pr_bundle.ix import (
    IX_COLUMNS,
    IxFile,
    IxRow,
    parse_ix,
    parse_ix_bundle,
)
from dataplatform.ingest.nse.pr_bundle.mcap import (
    MCAP_COLUMNS,
    McapFile,
    McapRow,
    parse_mcap,
    parse_mcap_bundle,
)

__all__ = [
    "ARCHIVE_START",
    "BC_COLUMNS",
    "IX_COLUMNS",
    "LOWERCASE_ERA_START",
    "MCAP_COLUMNS",
    "MCAP_ERA_START",
    "PR_BUNDLE_SOURCE_ID",
    "URL_TEMPLATE",
    "BcRow",
    "BundleMember",
    "IxFile",
    "IxRow",
    "McapFile",
    "McapRow",
    "MemberKind",
    "PrBundle",
    "parse_bc",
    "parse_bc_bundle",
    "parse_ix",
    "parse_ix_bundle",
    "parse_mcap",
    "parse_mcap_bundle",
    "url_for",
]
