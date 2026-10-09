"""M17.9 — the frozen keyword tables on NSE announcement subject lines.

Each category is pinned with real-looking subject lines (the L1 ``subject`` is NSE's ``desc``, the
``body`` its one-line ``attchmntText``; the shapes below are the lake's own) that must match, and
with near-misses that must not: an appointment is not a resignation, a GST order is not a SEBI
order, a rating *upgrade* or an investment-grade downgrade is not a sub-IG downgrade, a nil-default
quarterly return is not a default, the outcome of a board meeting is not an intimation of one.

The table is frozen: its digest is the sha256 of the file, the loader refuses an unknown category
or key, and the screens rule hash moves with it.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from analyst.commons.events import (
    EVENT_KEYWORDS_DIGEST,
    EVENT_KEYWORDS_PATH,
    EVENT_WATCH_CATEGORIES,
    INTEGRITY_CATEGORIES,
    classify,
    load_event_keywords,
    meeting_date,
)

TABLE = load_event_keywords()

# (subject, body) pairs. Company names are invented; the wording is the exchange's.
INTEGRITY_POSITIVE: dict[str, list[tuple[str, str | None]]] = {
    "auditor_resignation": [
        (
            "Resignation of Statutory Auditor",
            "Acme Lights Limited has informed the Exchange about Resignation of Statutory Auditor",
        ),
        (
            "Change in Auditors",
            "Acme Steel Limited has informed the Exchange regarding resignation of M/s ABC & Co. "
            "as Statutory Auditors of the company w.e.f. October 3, 2026.",
        ),
    ],
    "cfo_resignation": [
        (
            "Resignation",
            "Acme Tyres Limited has informed the Exchange regarding Resignation of Mr Rakesh Rao "
            "as Chief Financial Officer of the company w.e.f. October 06, 2026.",
        ),
        (
            "Resignation of Director/KMP/SMP",
            "Acme Decor Limited has informed the Exchange regarding Resignation of Raju Prasad "
            "(CFO) w.e.f. November 24, 2026.",
        ),
    ],
    "independent_director_resignation": [
        (
            "Resignation",
            "Acme Mills Limited has informed the Exchange regarding Resignation of Ms Rani Saxena "
            "as Non- Executive Independent Director of the company w.e.f. October 05, 2026.",
        ),
        (
            "Resignation",
            "Acme Heights Limited has informed the Exchange regarding Resignation of  Mr. Namdeo "
            "Khamitkar (DIN:07038714) as Non- Executive Independent Director of the company.",
        ),
    ],
    "sebi_order": [
        (
            "Action(s) taken or orders passed",
            "Acme Securities Limited has informed the Exchange that an interim order has been "
            "passed by the Securities and Exchange Board of India in the matter of the company.",
        ),
        (
            "Updates",
            "Acme Finance Limited has informed the Exchange that SEBI has passed an adjudication "
            "order imposing a penalty on the company.",
        ),
    ],
    "rating_downgrade_sub_ig": [
        (
            "Credit Rating- Revision",
            "Acme Infra Limited has informed the Exchange that CARE has downgraded its long-term "
            "rating to CARE BB+ (Negative) from CARE BBB-.",
        ),
        (
            "Credit Rating",
            "Acme Power Limited has informed the Exchange that ICRA has moved the rating to "
            "'Issuer Not Cooperating' category.",
        ),
    ],
    "default": [
        (
            "Defaults on Payment of Interest/Principal",
            "Defaults on Payment of Interest/Principal",
        ),
        (
            "Updates",
            "Acme Realty Limited has informed the Exchange that the company has defaulted on the "
            "repayment of its term loan instalment due on September 30, 2026.",
        ),
        (
            "Corporate Insolvency Resolution Process",
            "Acme Gears Limited has informed the Exchange about Corporate Insolvency Resolution "
            "Process",
        ),
    ],
}

INTEGRITY_NEGATIVE: list[tuple[str, str | None]] = [
    (
        "Appointment",
        "Acme Lab Limited has informed the Exchange regarding Appointment of Mr Shashi Bharuka "
        "as Chief Financial Officer of the company w.e.f. September 29, 2026.",
    ),
    (
        "Retirement",
        "Acme Funds Limited has informed the Exchange about Retirement of Mr Inder Ghuliani, as "
        "the Chief Financial Officer attaining the age of superannuation.",
    ),
    (
        "Resignation",
        "Acme Mills Limited has informed the Exchange regarding Resignation of Mrs Ranjana Mimani "
        "as Non- Executive Director of the company w.e.f. September 29, 2026.",
    ),
    (
        "Cessation",
        "Acme Health Care Limited has informed the Exchange regarding Cessation of Mr Sanjay "
        "Anand as Non- Executive Independent Director of the company w.e.f. September 29, 2026.",
    ),
    (
        "Resignation",
        "Acme Energy Limited has informed the Exchange regarding Resignation of Mrs.Sasi Raghu as "
        "Chairperson and Non Executive Non Independent Director of the company.",
    ),
    (
        "Change in Auditors",
        "Acme Jewels Limited has informed the Exchange regarding Change in Auditors of the "
        "company.",
    ),
    (
        "General Updates",
        "Acme Services Limited has informed the Exchange about change in Statutory Auditors of "
        "Acme Tech Private Limited, a material Subsidiary of the Company.",
    ),
    (
        "Resignation",
        "Acme Chem Limited has informed the Exchange regarding resignation of the Secretarial "
        "Auditor of the company.",
    ),
    (
        "Action(s) taken or orders passed",
        "Pursuant to Regulation 30 of the SEBI (Listing Obligations and Disclosure Requirements) "
        "Regulations, 2015 and SEBI Circular dated January 30, 2026, details of the Order received "
        "from Directorate of Large Enterprise, Mali are enclosed.",
    ),
    (
        "Action(s) initiated or orders passed",
        "Acme Pipes Limited has informed the Exchange that the Company has received an Order "
        "issued by the Deputy Commissioner of State Tax imposing a penalty.",
    ),
    (
        "Credit Rating- Revision",
        "Acme Bank has informed the Exchange about Credit Rating- Upgraded by India Ratings",
    ),
    (
        "Credit Rating- Revision",
        "Acme Hotels Limited has informed the Exchange that CRISIL has downgraded its rating to "
        "CRISIL A+ (Stable) from CRISIL AA-.",
    ),
    (
        "Credit Rating- Revision",
        "Acme Captab Limited has informed the Exchange that CARE has downgraded its rating to "
        "CARE BBB- (Stable).",
    ),
    (
        "Defaults on Payment of Interest/Principal",
        "Disclosure of defaults on payment of interest/ repayment of principal amount on loans "
        "from financial institutions for Quarter ended 30th September, 2026",
    ),
    (
        "Updates",
        "Acme Steel Limited has informed the Exchange that there has been no default in payment "
        "of interest on its non-convertible debentures.",
    ),
    (
        "Trading Window",
        "Acme Bank Limited has informed the Exchange about closure of trading window.",
    ),
]

WATCH_POSITIVE: dict[str, list[tuple[str, str | None]]] = {
    "buyback": [
        (
            "Buyback",
            "Acme Logistics Limited has informed the Exchange about Buyback of equity shares of "
            "the Company and matters incidental thereto.",
        ),
        (
            "Closure of Buy Back",
            "Acme Music Limited has informed the Exchange regarding Closure "
            "of Buy Back with effect from September 30, 2026",
        ),
    ],
    "bonus": [
        (
            "Bonus",
            "Acme Spintex Limited has informed the Exchange that the Board of Directors at its "
            "meeting held on September 29, 2026, have considered and approved bonus at the ratio "
            "of 1 : 1",
        ),
    ],
    "split": [
        (
            "Updates",
            "Acme Buildpro Limited has informed the Exchange regarding 'Intimation regarding "
            "Sub-Division/Split of Equity Shares'.",
        ),
        (
            "Record Date",
            "Acme Apparels Limited has informed the Exchange that Record date for the purpose of "
            "Split/Subdivision  is 10-Dec-2026.",
        ),
    ],
    "order_win": [
        (
            "Bagging/Receiving of orders/contracts",
            "Acme Projects Limited has informed the Exchange about Bagging/Receiving of "
            "orders/contracts",
        ),
        (
            "General Updates",
            "Acme Green Energy Limited has informed the Exchange regarding Receipt of Work Order "
            "worth approx. Rs. 2,025 crore for a Solar EPC Project.",
        ),
    ],
    "index_change": [
        (
            "General Updates",
            "Acme Chemicals Limited has informed the Exchange about inclusion of its equity shares "
            "in the NIFTY Midcap 150 index.",
        ),
    ],
    "results_board_meeting": [
        (
            "Board Meeting Intimation",
            "Acme Motors Limited has informed the Exchange about Board Meeting to be held on "
            "20-Oct-2026 to consider and approve the Quarterly Unaudited Financial Results.",
        ),
        (
            "General Updates",
            "Intimation of Board Meeting scheduled on October 21, 2026 to consider the financial "
            "results for the quarter ended September 30, 2026.",
        ),
    ],
}

WATCH_NEGATIVE: list[tuple[str, str | None]] = [
    (
        "Outcome of Board Meeting",
        "Acme Motors Limited has informed the Exchange regarding Outcome of Board Meeting held on "
        "October 20, 2026 approving the financial results.",
    ),
    (
        "Action(s) taken or orders passed",
        "Acme Media Limited has informed the Exchange about receipt of order passed by the State "
        "Tax officer, Commercial Taxes Department.",
    ),
    (
        "General Updates",
        "Acme Foods Limited has informed the Exchange about payment of performance bonus to "
        "employees.",
    ),
    (
        "Amalgamation/Merger",
        "Acme Holdings Limited has informed the Exchange about the scheme of demerger and split of "
        "the business undertaking.",
    ),
    (
        "Shareholders meeting",
        "Acme Plywoods Limited has informed the Exchange regarding Proceedings of Annual General "
        "Meeting held on September 28, 2026.",
    ),
]


@pytest.mark.parametrize(
    ("category", "subject", "body"),
    [(c, s, b) for c, cases in INTEGRITY_POSITIVE.items() for s, b in cases],
)
def test_each_integrity_category_matches_its_subject_lines(
    category: str, subject: str, body: str | None
) -> None:
    assert category in classify(subject, body, TABLE).integrity


@pytest.mark.parametrize(("subject", "body"), INTEGRITY_NEGATIVE)
def test_near_misses_are_not_integrity_events(subject: str, body: str | None) -> None:
    assert classify(subject, body, TABLE).integrity == ()


@pytest.mark.parametrize(
    ("category", "subject", "body"),
    [(c, s, b) for c, cases in WATCH_POSITIVE.items() for s, b in cases],
)
def test_each_event_watch_category_matches_its_subject_lines(
    category: str, subject: str, body: str | None
) -> None:
    assert category in classify(subject, body, TABLE).event_watch


@pytest.mark.parametrize(("subject", "body"), WATCH_NEGATIVE)
def test_near_misses_are_not_event_watch_facts(subject: str, body: str | None) -> None:
    found = classify(subject, body, TABLE).event_watch
    assert "results_board_meeting" not in found
    assert "order_win" not in found
    assert "bonus" not in found
    assert "split" not in found


def test_every_category_has_a_positive_case() -> None:
    assert set(INTEGRITY_POSITIVE) == set(INTEGRITY_CATEGORIES)
    assert set(WATCH_POSITIVE) == set(EVENT_WATCH_CATEGORIES)


def test_the_table_is_frozen_by_the_digest_of_its_bytes() -> None:
    assert hashlib.sha256(EVENT_KEYWORDS_PATH.read_bytes()).hexdigest() == EVENT_KEYWORDS_DIGEST
    assert TABLE.digest == EVENT_KEYWORDS_DIGEST


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kw.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_loader_refuses_a_changed_category_set_or_an_unknown_key(tmp_path: Path) -> None:
    original = EVENT_KEYWORDS_PATH.read_text(encoding="utf-8")
    dropped = original.replace("  cfo_resignation:", "  cfo_exit:")
    with pytest.raises(ValueError, match="categories"):
        load_event_keywords(_write(tmp_path, dropped))
    unknown = original.replace("      unless: '\\bsubsidiar'\n", "      except: 'x'\n", 1)
    with pytest.raises(ValueError, match="unknown keys"):
        load_event_keywords(_write(tmp_path, unknown))
    broken = original.replace("'\\bbuy[- ]?\\s?back\\b'", "'(unclosed'")
    with pytest.raises(ValueError, match="does not compile"):
        load_event_keywords(_write(tmp_path, broken))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Board Meeting to be held on 20-Oct-2026 to consider results", date(2026, 10, 20)),
        ("Board Meeting scheduled on October 21, 2026 to consider", date(2026, 10, 21)),
        ("meeting of the Board will be held on 22nd October, 2026", date(2026, 10, 22)),
        ("Board meeting to be held on 23/10/2026 for results", date(2026, 10, 23)),
        ("Board Meeting intimation for results", None),
        ("meeting held on 31/02/2026", None),
    ],
)
def test_the_meeting_date_is_read_from_the_text_or_left_unknown(
    text: str, expected: date | None
) -> None:
    assert meeting_date(text) == expected
