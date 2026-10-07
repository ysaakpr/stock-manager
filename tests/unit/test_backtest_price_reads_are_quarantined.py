"""Every price read under `backtest/` goes through the D22 quarantine (M13.4).

`dataplatform.query.PriceQuarantine` withholds a quarantined ISIN's bars before its unsourced
share-basis step. It only works if every backtest reader of L1/L2 prices asks for it, and a new
reader added with `store.l2.register_raw_view` would silently read the phantom step again. This
guard reads the source (AST, no imports) and fails when:

* a module under `backtest/` imports a raw price accessor from the store (`register_raw_view`,
  `register_adjusted_view`, `read_adjusted`, `read_prices_raw`, `l1_partition_path`), outside
  the allowlist below, each entry of which carries its reason;
* a view registrar is called on anything other than a quarantine (`quarantine.…` or
  `default_price_quarantine().…`), outside the allowlist;
* any of the known quarantined registrations disappears (`_EXPECTED`): removing the wiring in
  `_L1Reader`, `_SwingFeatures`, `forecast_run._FeatureCursor` or `LiquidityRankTiers` fails here;
* an `_L1Reader` per-session `read_parquet($path)` query loses its `self._admits` predicate.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

BACKTEST: Final = Path(__file__).resolve().parents[2] / "backtest"

_RAW_ACCESSORS: Final = frozenset(
    {
        "register_raw_view",
        "register_adjusted_view",
        "read_adjusted",
        "read_prices_raw",
        "l1_partition_path",
    }
)
_REGISTRARS: Final = frozenset({"register_raw_view", "register_adjusted_view"})

#: `(module, accessor) -> reason`. Only reads that are not decisions, or that filter themselves.
_ALLOWED_IMPORTS: Final[dict[tuple[str, str], str]] = {
    ("tax_report.py", "register_raw_view"): (
        "Sec 55(2)(ac) grandfathering FMV on 2018-01-31: a statutory valuation of the price as "
        "quoted, read after the run for the tax report, never by a decision"
    ),
    ("tax_report.py", "read_prices_raw"): "the same 2018-01-31 FMV read, per session",
    ("band_hits.py", "l1_partition_path"): (
        "reads only (symbol, series, isin) to map the price-band file to ISINs; no price column"
    ),
    ("run.py", "l1_partition_path"): (
        "_L1Reader's per-session partition reads, each filtered by self._admits (checked below)"
    ),
}

#: Bare (non-quarantine) registrar calls permitted: `(module, registrar)`.
_ALLOWED_BARE_CALLS: Final = frozenset({("tax_report.py", "register_raw_view")})

#: Every quarantined registration that must exist: `(module, class, registrar)`.
_EXPECTED: Final = frozenset(
    {
        ("run.py", "_L1Reader", "register_raw_view"),
        ("run.py", "_SwingFeatures", "register_raw_view"),
        ("run.py", "_SwingFeatures", "register_adjusted_view"),
        ("forecast_run.py", "_FeatureCursor", "register_raw_view"),
        ("forecast_run.py", "_FeatureCursor", "register_adjusted_view"),
        ("cap_tiers.py", "LiquidityRankTiers", "register_raw_view"),
    }
)


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(BACKTEST)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(BACKTEST.rglob("*.py"))
    ]


def _is_quarantine(receiver: ast.expr) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id == "quarantine"
    return (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "default_price_quarantine"
    )


def _registrations() -> tuple[set[tuple[str, str, str]], list[str]]:
    """Quarantined registrations by (module, class, registrar), and every bare call found."""
    found: set[tuple[str, str, str]] = set()
    bare: list[str] = []
    for module, tree in _modules():
        owner = {
            id(node): cls.name
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for node in ast.walk(cls)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _REGISTRARS:
                if _is_quarantine(func.value):
                    found.add((module, owner.get(id(node), "<module>"), func.attr))
                else:
                    bare.append(f"{module}:{node.lineno} {ast.unparse(func)}")
            elif (
                isinstance(func, ast.Name)
                and func.id in _REGISTRARS
                and (module, func.id) not in _ALLOWED_BARE_CALLS
            ):
                bare.append(f"{module}:{node.lineno} {func.id}")
    return found, bare


def test_no_backtest_module_imports_a_raw_price_accessor_outside_the_allowlist() -> None:
    offending = [
        f"{module}: {alias.name} from {node.module}"
        for module, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("dataplatform.store")
        for alias in node.names
        if alias.name in _RAW_ACCESSORS and (module, alias.name) not in _ALLOWED_IMPORTS
    ]
    assert not offending, f"read prices through dataplatform.query.PriceQuarantine: {offending}"


def test_every_view_registrar_call_goes_through_the_quarantine() -> None:
    _, bare = _registrations()
    assert not bare, f"unquarantined price views: {bare}"


def test_every_known_quarantined_registration_is_still_wired() -> None:
    found, _ = _registrations()
    missing = _EXPECTED - found
    assert not missing, f"quarantine wiring removed: {sorted(missing)}"


def test_every_l1_reader_partition_read_carries_the_quarantine_predicate() -> None:
    tree = dict(_modules())["run.py"]
    [reader] = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_L1Reader"]
    queries = [
        ast.unparse(node)
        for node in ast.walk(reader)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and "read_parquet($path)" in ast.unparse(node)
    ]
    assert len(queries) == 4  # closes, fill bars, BE/BZ bars, most-liquid
    unfiltered = [q for q in queries if "self._admits" not in q]
    assert not unfiltered, f"partition read without the quarantine predicate: {unfiltered}"


def test_every_allowlist_entry_is_still_needed() -> None:
    """A stale exemption is how a later unquarantined read slips in under an old reason."""
    used = {
        (module, alias.name)
        for module, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert set(_ALLOWED_IMPORTS) <= used
