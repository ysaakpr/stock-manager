"""A5: monitoring engine (T0/T1/T2).

The tiers of §5.4, cheapest first. T0 (M5.11) is the mechanical daily sweep and the data-red
interlock in front of it: it runs a fixed battery of ~₹0 checks over a case's holdings, records a
heartbeat when nothing fires, escalates each flag to T1 when one does, and — before any of that —
refuses to run at all on a day the data platform is not green (invariant #10). T1 (triggered LLM
review) and T2 (scheduled deep review) build on this package in M6.

The public surface is the T0 monitor, its inputs and outputs, and the escalation queue T1 will
read; the pure per-check functions are exported too, since each is independently testable and the
daily loop may compose them directly.
"""

from analyst.monitor.interlock import (
    CORE_DATASETS,
    GreenGate,
    GreenLike,
    StatusApiGate,
)
from analyst.monitor.t0 import (
    CHECKS_PERFORMED,
    CorporateActionEvent,
    Deal,
    DeliverySignal,
    EscalationQueue,
    FlowKind,
    InMemoryEscalationQueue,
    KeywordWatch,
    T0Check,
    T0Config,
    T0Escalation,
    T0Flag,
    T0Holding,
    T0Inputs,
    T0Monitor,
    T0Outcome,
    T0Result,
    check_announcements,
    check_corporate_actions,
    check_data_quality,
    check_drawdown,
    check_flow,
    check_rails,
)

__all__ = [
    "CHECKS_PERFORMED",
    "CORE_DATASETS",
    "CorporateActionEvent",
    "Deal",
    "DeliverySignal",
    "EscalationQueue",
    "FlowKind",
    "GreenGate",
    "GreenLike",
    "InMemoryEscalationQueue",
    "KeywordWatch",
    "StatusApiGate",
    "T0Check",
    "T0Config",
    "T0Escalation",
    "T0Flag",
    "T0Holding",
    "T0Inputs",
    "T0Monitor",
    "T0Outcome",
    "T0Result",
    "check_announcements",
    "check_corporate_actions",
    "check_data_quality",
    "check_drawdown",
    "check_flow",
    "check_rails",
]
