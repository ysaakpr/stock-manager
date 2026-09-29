"""X2 — the swing and forecast feature queries hand the price to the policy exactly.

Both queries used to ``CAST(... AS DOUBLE)`` the close before it became ``SwingRecord.price`` /
the forecast row's price — the number whole-share sizing and the trailing stop read. The casts are
gone and ``_exact_price`` refuses anything but a ``Decimal``, so a cast that comes back fails loud.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from backtest.run import _exact_price

_ROOT = Path(__file__).resolve().parents[2]


def test_a_decimal_price_passes_through_unchanged() -> None:
    price = Decimal("1234.5500")
    assert _exact_price(price) is price


def test_a_float_price_is_refused() -> None:
    with pytest.raises(TypeError):
        _exact_price(1234.55)


@pytest.mark.parametrize("module", ["backtest/run.py", "backtest/forecast_run.py"])
def test_no_feature_query_casts_to_double(module: str) -> None:
    assert "AS DOUBLE" not in (_ROOT / module).read_text(encoding="utf-8")
