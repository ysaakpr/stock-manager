"""D7: data quality sentinel.

The package's public surface starts with the gap report (M1.11) — the module that answers "which
days do we not have data for, and why", which is the M1 gate's "100% of missing days explained"
criterion in code. `GapReport.fully_explained` is that criterion as one boolean.
"""

from dataplatform.quality.gaps import (
    PER_SESSION_CADENCES,
    GapEntry,
    GapReason,
    GapReport,
    GapReportError,
    GapScanner,
    L1Check,
    L1Presence,
    L1Result,
    LakeL1Presence,
    SourceExpectation,
    build_report,
    classify_pair,
    expectations_from_register,
)

__all__ = [
    "PER_SESSION_CADENCES",
    "GapEntry",
    "GapReason",
    "GapReport",
    "GapReportError",
    "GapScanner",
    "L1Check",
    "L1Presence",
    "L1Result",
    "LakeL1Presence",
    "SourceExpectation",
    "build_report",
    "classify_pair",
    "expectations_from_register",
]
