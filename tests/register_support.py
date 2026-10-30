"""Test support: the Source Register with its DECLINED rows turned back into ordinary FAILED ones.

For tests of rules that must hold for a row whatever its status — robots refusals, spacing, the
crawler's URL shapes. The checked-in screener row is DECLINED (HUMAN_DECISIONS D12, via D19), so
`resolve_policy` and `ScreenerCrawler.from_register` refuse it by design; these tests exercise the
same row's robots and spacing rules through an un-declined copy. Paused rows (HUMAN_DECISIONS D29)
are refused and un-paused the same way. Test-only by construction (it lives under
``tests/``): no product path can un-decline or un-pause a source.
"""

from __future__ import annotations

from dataplatform.ingest.source_register import SourceRegister, Status


def undeclined(register: SourceRegister) -> SourceRegister:
    """A re-validated copy of `register` in which every DECLINED row is FAILED with no record and
    every paused row (D29) is un-paused — for rules that must hold whatever a row's standing."""
    sources = [
        s.model_copy(update={"status": Status.FAILED, "declined": None}) if s.is_declined else s
        for s in register.sources
    ]
    sources = [s.model_copy(update={"paused": None}) if s.is_paused else s for s in sources]
    return SourceRegister.model_validate(
        register.model_copy(update={"sources": sources}).model_dump()
    )


def unpaused(register: SourceRegister) -> SourceRegister:
    """A re-validated copy of `register` with every pause record (D29) removed, nothing else.

    For tests of a paused source's own leg — the BSE announcement pages, the BSE CA requests, the
    scrip-master snapshot — which must keep working the day the owner lifts the pause. The paused
    behaviour itself is tested against the checked-in register (`tests/unit/test_bse_pause.py`).
    """
    sources = [
        s.model_copy(update={"paused": None}) if s.is_paused else s for s in register.sources
    ]
    return SourceRegister.model_validate(
        register.model_copy(update={"sources": sources}).model_dump()
    )
