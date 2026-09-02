# Golden corporate-action suite — reference B

The highest-value tests in the platform (EXECUTION_PLAN §4.3, the M2 gate). This suite is
**reference B** of the two independent references ratified in AGENTIC_CONTEXT §2 (B2): adjusted
closes **recomputed by hand from the published CA terms** and checked in as literal `Decimal`
values. Reference A — `yfinance` adjusted closes for the `.NS` ticker — lives in M2.7
(`test_yfinance_reference.py`). The two are independent *in method*, so a shared-direction error in
a third-party adjusted series cannot pass both.

```
tests/golden/
  casebook.py            the GoldenCase shape, action builders, and case discovery
  test_golden_ca.py      the harness — case-agnostic; you never edit it to add a case
  cases/                 one self-contained file per case
    irctc_split_2021.py
    ril_bonus_2024.py
    ...
```

Run it:

```bash
uv run pytest tests/golden -q
```

## What each test proves

- `test_all_seven_named_cases_present` — the seven §4.3-named cases exist as files.
- `test_adjusted_closes_match_hand_computed` — the M2.4 engine reproduces every checked-in literal.
  This is the primary guard against an inverted factor convention: adjust by the reciprocal and a
  split's pre-ex close comes out multiplied instead of divided, and the literal no longer matches.
- `test_golden_literals_are_direction_sensitive` + `test_suite_contains_a_direction_discriminating_case`
  — make "fails loudly if the factor convention is inverted" a *property of the suite*: the literals
  cannot be reproduced under a flipped (reciprocal) convention, and at least one split/bonus case
  always exists to discriminate direction (a suite of only structural breaks, whose factors are 1,
  could not).
- `test_returns_bridge_exactly_the_structural_breaks` — a merger/demerger/DVR ex-date is bridged in
  the return series (`ret=None`), every other day carries a real return (§4.3 rule 3).

## The factor convention (what a literal must reflect)

Back-adjusted, current-basis (full statement in `dataplatform/corpactions/factors.py`):

- **Split** face value `from → to`: `price_factor = to / from`. A 1:5 split (`Rs.10 → Rs.2`) gives
  `0.2`; a raw pre-ex close of `4285.00` adjusts to `4285.00 × 0.2 = 857.00`.
- **Bonus** `new:held`: `price_factor = held / (new + held)`. A 1:1 bonus gives `0.5`.
- Closes **on or after** the ex-date are already in current basis (factor `1.0`); only closes
  **strictly before** it are rescaled.
- **Merger / demerger / DVR conversion**: a *structural break*, not a scaling. `price_factor = 1`
  (the adjusted level series equals the raw series, gap and all — adjusting for the other entity
  needs a different ISIN's price and is not guessed), and the **return** across the ex-date is
  bridged.
- **Dividends** do not appear in the price-adjusted series (they belong to the total-return series);
  none of the seven named cases is a dividend case.

## Adding a case (one file, no harness change)

The ~13 ugly cases found during backfill are added exactly this way. Create
`cases/<descriptive_id>.py` that defines a module-level `CASE: GoldenCase`. Discovery walks the
`cases/` package, so no registration is needed.

A split/bonus case:

```python
from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, split  # or `bonus`

_ISIN = "INE000000000"
_EX = date(2023, 5, 10)

CASE = GoldenCase(
    case_id="acme_split_2023",
    title="ACME 1:2 face-value split (2023)",
    isin=_ISIN,
    published_terms="Face value split from Rs.10 to Rs.5 (1:2), ex-date 2023-05-10",
    actions=(split(_ISIN, _EX, fv_from="10", fv_to="5", raw_text="FV SPLIT FROM RS.10 TO RS.5"),),
    expectations=(
        # price_factor = 5 / 10 = 0.5
        Expectation(date(2023, 5, 9), Decimal("900.00"), Decimal("450.00"), "900.00 x 0.5"),
        Expectation(_EX, Decimal("455.00"), Decimal("455.00"), "455.00 x 1.0"),
    ),
)
```

A structural-break case (merger/demerger/DVR): use `merger`, `demerger`, or `dvr_conversion`;
adjusted closes equal raw closes (unit factor); list the ex-date in `bridged_ex_dates`.

Rules for a good case:

1. **Literals, not computations.** Write the adjusted close as a `Decimal` literal and put the
   arithmetic that yields it in the `Expectation.arithmetic` string and the module docstring. Never
   call the factor engine to produce an expected value — that would collapse reference B into the
   thing it checks.
2. **Money is `Decimal`.** Never `float`.
3. **Include at least one close strictly before the ex-date and one on/after it**, so the case
   actually exercises the factor boundary. For a structural break also include a post-ex day so the
   bridge and the following real return are both asserted.
4. **Raw closes are representative** EOD levels around the ex-date; the load-bearing invariant under
   test is `adjusted = raw × cumulative_factor` derived from the published terms, so internal
   consistency (literal = raw × factor) is what matters, checked against the engine's own output.
