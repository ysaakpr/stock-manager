"""A2: the §5.1 INTERVIEW / case builder — elicit the inputs, recommend the policies, one proposal.

§5.1 opens a case in two moves and ends them in one act. First the analyst *elicits* the facts only
the human can give — capital plan, horizon, theme, risk appetite, exclusions. Then it *recommends*
the full §5.2 ratified policy set derived from those facts, each recommendation carrying the
reasoning that ties it to what was stated. The output is a single PROPOSAL — universe with purity
scores, per-holding theses, rotation dial, rails, exit menu, cash policy, benchmark pair — that a
human ratifies in one act, never a series of approvals.

Two packages, proved in `tests/unit/test_interview.py`:

* `flow.py` — the interview and the recommendation. `conduct_interview` turns a scripted transcript
  into validated `InterviewAnswers`; `recommend_policies` derives all seven §5.2 policies from them,
  the rotation dial from the stated risk appetite and the rails from the concentration tolerance,
  each with a recorded `Recommendation` note (acceptance 1, 2).
* `proposal.py` — the document. `build_proposal` folds the recommended policies, the theme map's
  purity-scored universe (A3) and a §5.3 thesis per holding (A4) into one `Proposal` with a single
  content hash and a single `ratified_with` — one document, one ratification (acceptance 3).

The dependencies are reused, not reimplemented: the universe and purity scores come from
`analyst.mapper`, the theses from `analyst.thesis`, the policy objects and the ratification artifact
from `analyst.cases`. Nothing here reads a clock or the network — the proposal is a deterministic
function of the interview and its inputs; the only time in it is the ratification's, from an
injected `Clock` at the moment of approval (B10).
"""

from analyst.interview.flow import (
    DEFAULT_BENCHMARK_PRIMARY,
    DEFAULT_PARKING_ISIN,
    DEFAULT_PARKING_SYMBOL,
    INTERVIEW_QUESTIONS,
    ConcentrationTolerance,
    IncompleteInterviewError,
    InterviewAnswers,
    InterviewError,
    InterviewField,
    InterviewParseError,
    InterviewQuestion,
    Recommendation,
    RecommendedPolicies,
    RiskAppetite,
    conduct_interview,
    recommend_capital_plan,
    recommend_cash_policy,
    recommend_exit_menu,
    recommend_horizon,
    recommend_monitoring,
    recommend_policies,
    recommend_rails,
    recommend_rotation_dial,
)
from analyst.interview.proposal import (
    PROPOSAL_CONTENT_FIELDS,
    EmptyUniverseError,
    IncompleteProposalError,
    Proposal,
    ProposalError,
    ProposalRatificationMismatchError,
    ProposalStatus,
    build_proposal,
)

__all__ = [
    "DEFAULT_BENCHMARK_PRIMARY",
    "DEFAULT_PARKING_ISIN",
    "DEFAULT_PARKING_SYMBOL",
    "INTERVIEW_QUESTIONS",
    "PROPOSAL_CONTENT_FIELDS",
    "ConcentrationTolerance",
    "EmptyUniverseError",
    "IncompleteInterviewError",
    "IncompleteProposalError",
    "InterviewAnswers",
    "InterviewError",
    "InterviewField",
    "InterviewParseError",
    "InterviewQuestion",
    "Proposal",
    "ProposalError",
    "ProposalRatificationMismatchError",
    "ProposalStatus",
    "Recommendation",
    "RecommendedPolicies",
    "RiskAppetite",
    "build_proposal",
    "conduct_interview",
    "recommend_capital_plan",
    "recommend_cash_policy",
    "recommend_exit_menu",
    "recommend_horizon",
    "recommend_monitoring",
    "recommend_policies",
    "recommend_rails",
    "recommend_rotation_dial",
]
