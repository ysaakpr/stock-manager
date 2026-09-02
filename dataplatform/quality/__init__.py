"""D7: data quality sentinel.

The package's public surface starts with the gap report (M1.11) — the module that answers "which
days do we not have data for, and why", which is the M1 gate's "100% of missing days explained"
criterion in code. `GapReport.fully_explained` is that criterion as one boolean.

The second half is the value sentinel (M2.8): the gap report is about *missing* days, the sentinel
about *wrong values*. `run_sentinel` runs a registry of rules over gathered facts and returns
`QualityFinding`s; `persist_findings` lands them in `quality_flag`, where `read_quality`
(`/status/quality`) serves them and `SyncStateStore.open_error_flags` gates trading on them. The
charter rule is the unexplained-move tripwire; new rules are one file each under `rules/`.
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
from dataplatform.quality.sentinel import (
    CloseToCloseMove,
    ExchangeClose,
    PersistFindingCounts,
    QualityFinding,
    SentinelInput,
    SentinelRule,
    Severity,
    default_rules,
    finding_fingerprint,
    moves_from_price_rows,
    persist_findings,
    register,
    registered_rules,
    run_sentinel,
)

__all__ = [
    "PER_SESSION_CADENCES",
    "CloseToCloseMove",
    "ExchangeClose",
    "GapEntry",
    "GapReason",
    "GapReport",
    "GapReportError",
    "GapScanner",
    "L1Check",
    "L1Presence",
    "L1Result",
    "LakeL1Presence",
    "PersistFindingCounts",
    "QualityFinding",
    "SentinelInput",
    "SentinelRule",
    "Severity",
    "SourceExpectation",
    "build_report",
    "classify_pair",
    "default_rules",
    "expectations_from_register",
    "finding_fingerprint",
    "moves_from_price_rows",
    "persist_findings",
    "register",
    "registered_rules",
    "run_sentinel",
]
