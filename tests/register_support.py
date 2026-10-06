"""Test support: the Source Register with its DECLINED rows turned back into ordinary FAILED ones.

For tests of rules that must hold for a row whatever its status — robots refusals, spacing, the
crawler's URL shapes. The checked-in screener row is DECLINED (HUMAN_DECISIONS D12, via D19), so
`resolve_policy` and `ScreenerCrawler.from_register` refuse it by design; these tests exercise the
same row's robots and spacing rules through an un-declined copy. Test-only by construction (it lives
under ``tests/``): no product path can un-decline a source.
"""

from __future__ import annotations

from dataplatform.ingest.source_register import SourceRegister, Status


def undeclined(register: SourceRegister) -> SourceRegister:
    """A re-validated copy of `register` in which every DECLINED row is FAILED with no record."""
    sources = [
        s.model_copy(update={"status": Status.FAILED, "declined": None}) if s.is_declined else s
        for s in register.sources
    ]
    return SourceRegister.model_validate(
        register.model_copy(update={"sources": sources}).model_dump()
    )
