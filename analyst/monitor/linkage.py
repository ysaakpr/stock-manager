"""A5 · News → holding linkage: which held ISIN, if any, a news item is about (§5.4, M6.2).

The GDELT and RSS feeds (M6.1) land as `NewsRow`s that carry a headline and tagged entities
but **no ISIN** — an article is about a company by name, not by identity. The T0 monitor cannot
match a news item against a holding's break conditions until it knows *which* holding the item
concerns, and the one legal join key is the ISIN (invariant #2). This module is that join: it
turns "who is this news about" into "which of our held ISINs, if any".

It resolves against a bounded working set — the ISINs actually held in the cases under review —
and never against the whole universe, for two reasons. It is far cheaper (a few surface forms
per name, compiled once), and it is far safer: precision, not recall, is what governs T0
escalation cost (§5.4 — a noisy matcher makes T1 expensive), and a matcher that only ever
considers names we hold cannot invent a link to a name we do not. The surface forms come from
three places:

* the **identity master** (D2): the security's current legal name, and its exchange symbols
  across *every* exchange it lists on — which is what makes dual-listed (NSE + BSE) names
  resolve, since the master carries a symbol window per exchange and this reads them all;
* the **name minus its corporate suffix** ("Eicher Motors Limited" → "Eicher Motors"), because
  a headline says the trading name, not the legal one;
* the **curated alias table** (`dataplatform/identity/aliases.yaml`): brands, market short
  forms, and the names a company traded under before a rename or merger — the hard cases the
  master cannot know.

Three precision guards keep the join from being the noisy matcher the plan warns against:

* **Whole-word, phrase-anchored matching.** A form matches only at word boundaries on both
  sides, so "Infosys" never fires inside "Infosystems" and the phrase "Reliance Industries"
  never fires on the ordinary word "reliance" in "reliance on imports". This is a stricter
  matcher than the announcement keyword one (which is stem-open on the right by design); a
  *name* is an exact thing, a break condition's keyword is a stem.
* **No bare common words or too-short derived tickers.** A single-token form that is an ordinary
  word is dropped, and an exchange ticker shorter than four characters is not derived (a curator
  may still add a vetted short form like "TCS" to the alias table, where the judgement is
  explicit).
* **Ambiguity is dropped, not guessed.** If one surface form would resolve to more than one held
  ISIN, that form links to neither — the identity master's own rule (an ambiguous identity is
  never a pick) applied to names.

**Measured on the labelled sample** (`tests/fixtures/news_linkage/labelled_sample.yaml`,
60 items — 44 that should link to a held ISIN, 16 distractors that should not), over the
individual (item → ISIN) pairs (47 true pairs in total): **precision 1.00, recall 0.91** —
43 true links found, 0 false links, 4 missed. The four misses are headlines that name a company
only by description or by an un-curated short form ("Bengaluru IT bellwether", "maker of the
Classic 350"); recall is traded for precision deliberately, and the fix for a persistent miss is
a curated alias entry, not a looser matcher. `tests/unit/test_linkage.py` recomputes these and
fails if precision drops below 0.95 or recall below 0.85.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from dataplatform.identity.master import Exchange, IdentityMaster, UnknownIsinError
from dataplatform.ingest.news import NewsRow
from dataplatform.logging import get_logger
from dataplatform.query.announcement_search import normalize

__all__ = [
    "ALIASES_PATH",
    "AliasEntry",
    "AliasTable",
    "MatchKind",
    "NameResolver",
    "NewsLink",
    "SurfaceForm",
    "load_aliases",
]

_LOG = get_logger(__name__)

#: The curated alias table, checked in beside the identity master it augments.
ALIASES_PATH: Final[Path] = Path(__file__).parents[2] / "dataplatform" / "identity" / "aliases.yaml"

#: Corporate suffixes stripped to get a headline-form of a legal name. Order does not matter; each
#: is removed from the end. Kept deliberately small — only endings that are pure legal
#: furniture, never a word that could carry meaning ("Industries", "Motors" and the like stay).
_CORPORATE_SUFFIXES: Final[tuple[str, ...]] = (
    "limited",
    "ltd",
    "plc",
    "corporation",
    "corp",
    "company",
    "co",
    "incorporated",
    "inc",
)

#: Single-token derived forms that are ordinary words are dropped, however they arose — a name
#: whose distinctive part collides with English must be curated (an alias entry), not matched
#: bare. Small and universe-aware on purpose; the alias table is where the harder judgements live.
_COMMON_WORDS: Final[frozenset[str]] = frozenset(
    {
        "reliance",
        "india",
        "power",
        "motors",
        "industries",
        "enterprises",
        "services",
        "tata",
        "adani",
        "united",
        "century",
    }
)

#: An exchange ticker shorter than this is not derived from the master — too collision-prone to
#: match on its own (a curator may still add a vetted short form to the alias table).
_MIN_DERIVED_TICKER_LEN: Final = 4


class MatchKind(StrEnum):
    """How a news item was tied to an ISIN — recorded so an escalation can say why it linked."""

    NAME = "name"
    """The security's current legal name, or that name minus its corporate suffix (from the
    master)."""

    TICKER = "ticker"
    """An exchange symbol from one of the ISIN's listing windows (any exchange — dual-listing)."""

    ALIAS = "alias"
    """A curated brand or market short form from the alias table."""

    FORMER_NAME = "former_name"
    """A curated name the company traded under before a rename or merger."""


class AliasEntry(BaseModel):
    """One security's curated equivalences — the names a feed uses that the master cannot know.

    What it does: carry, for one ISIN, the former names and market short forms that should link a
    news item to it. What it assumes: every form is a human-vetted, high-confidence equivalence (the
    file's contract). What it never does: hold a form the master already gives — that is
    duplication, and the resolver would produce it anyway.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(min_length=1, description="the identity these forms resolve to")
    canonical: str = Field(min_length=1, description="current legal name, for the reader")
    former_names: tuple[str, ...] = Field(
        default=(), description="names traded under before a rename/merger"
    )
    also_known_as: tuple[str, ...] = Field(
        default=(), description="brands and market short forms not derivable from the legal name"
    )
    note: str = Field(default="", description="why this curated entry is needed")


class AliasTable(BaseModel):
    """The whole `aliases.yaml`: a version and the curated per-ISIN entries, indexed by ISIN."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(description="schema version of the alias file")
    aliases: tuple[AliasEntry, ...] = Field(default=(), description="the curated entries")

    def by_isin(self, isin: str) -> AliasEntry | None:
        """The curated entry for an ISIN, or `None` — the resolver's lookup for one holding."""
        for entry in self.aliases:
            if entry.isin == isin:
                return entry
        return None


def load_aliases(path: Path = ALIASES_PATH) -> AliasTable:
    """Parse and type-check the curated alias table. Raises on a schema break or a missing file."""
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return AliasTable.model_validate(raw)


@dataclass(frozen=True, slots=True)
class SurfaceForm:
    """One compiled name/ticker/alias form for one ISIN — the unit the resolver matches with.

    `text` is the human form (for the escalation's audit trail); `pattern` is its
    whole-word-anchored matcher; `kind` records where it came from. Frozen and hashable so the
    ambiguity guard can
    compare forms across ISINs by their normalized text.
    """

    isin: str
    text: str
    kind: MatchKind
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class NewsLink:
    """One resolved link: a news item is about a held ISIN, and how we know.

    Carries the row, the ISIN, the surface form that fired and its kind — everything an escalation
    needs to state *why* a news item was tied to a holding without re-running the match.
    """

    news_row: NewsRow
    isin: str
    matched_form: str
    kind: MatchKind


def _compile_form(text: str) -> re.Pattern[str] | None:
    """Compile a name/ticker/alias into a whole-word-anchored, case-insensitive matcher.

    Anchored with `\\b` on *both* sides — unlike the announcement keyword matcher, which is
    stem-open on the right: a name is an exact thing, so "Infosys" must not fire in "Infosystems".
    Internal whitespace becomes `\\s+` so a phrase matches however the source spaced it, and each
    token is `re.escape`d so a name containing regex metacharacters (an ampersand) is literal.
    Returns `None` for an empty/whitespace form rather than raising — a blank alias is a data
    slip to skip, not a crash.
    """
    normalized = normalize(text)
    if not normalized:
        return None
    escaped = r"\s+".join(re.escape(token) for token in normalized.split())
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)


def _strip_suffix(name: str) -> str | None:
    """A legal name with its trailing corporate suffix removed, or `None` if that changes nothing.

    "Eicher Motors Limited" → "Eicher Motors"; "Infosys" → `None` (no suffix to strip). Only the
    last token is considered — "Motors" in the middle of a name is a word, not furniture.
    """
    tokens = normalize(name).split()
    if len(tokens) > 1 and tokens[-1] in _CORPORATE_SUFFIXES:
        return " ".join(tokens[:-1])
    return None


def _is_droppable_single_token(text: str) -> bool:
    """Whether a single-token form is an ordinary word that must not be matched bare (precision)."""
    tokens = normalize(text).split()
    return len(tokens) == 1 and tokens[0] in _COMMON_WORDS


class NameResolver:
    """Per-ISIN surface forms for a bounded set of held ISINs — the news→holding matcher.

    What it does: hold, for each held ISIN, the compiled name/ticker/alias forms that identify it,
    and answer "which held ISINs is this news row about". What it assumes: the ISIN set is the
    working set
    of held names (bounded and small), and the identity master and alias table it was built from are
    the point-in-time truth for those names. What it never does: guess. A form that would resolve to
    more than one held ISIN is dropped at build time, so an ambiguous name links to nothing rather
    than to an arbitrary holding — the master's own rule, applied to names.
    """

    __slots__ = ("_forms_by_isin",)

    def __init__(self, forms_by_isin: Mapping[str, tuple[SurfaceForm, ...]]) -> None:
        self._forms_by_isin = {isin: tuple(forms) for isin, forms in forms_by_isin.items()}

    def __repr__(self) -> str:
        forms = sum(len(forms) for forms in self._forms_by_isin.values())
        return f"{type(self).__name__}(isins={len(self._forms_by_isin)}, forms={forms})"

    @classmethod
    def build(
        cls,
        isins: Iterable[str],
        master: IdentityMaster,
        aliases: AliasTable,
        *,
        exchanges: Sequence[Exchange] = (Exchange.NSE, Exchange.BSE),
    ) -> NameResolver:
        """Build the resolver for `isins` from the identity master and the curated alias table.

        For each ISIN it gathers the legal name and its suffix-stripped form (from the master),
        every exchange symbol of length >= 4 across `exchanges` (which is what resolves dual-listed
        names — the master carries one symbol window per exchange and this reads them all), and
        every curated former name and short form from the alias table. Ordinary-word single tokens
        are dropped, and any form shared by two held ISINs is dropped from both — the ambiguity
        guard — so a build never yields a form that could mis-link.
        """
        candidate: dict[str, list[SurfaceForm]] = {}
        for isin in isins:
            forms: list[tuple[str, MatchKind]] = []
            try:
                security = master.security(isin)
            except UnknownIsinError:
                security = None
            if security is not None:
                forms.append((security.name, MatchKind.NAME))
                stripped = _strip_suffix(security.name)
                if stripped is not None:
                    forms.append((stripped, MatchKind.NAME))
            seen_symbols: set[str] = set()
            for exchange in exchanges:
                for window in master.windows_for(isin):
                    if window.exchange is not exchange:
                        continue
                    symbol = window.symbol
                    if len(symbol) >= _MIN_DERIVED_TICKER_LEN and symbol not in seen_symbols:
                        seen_symbols.add(symbol)
                        forms.append((symbol, MatchKind.TICKER))
            entry = aliases.by_isin(isin)
            if entry is not None:
                forms.extend((name, MatchKind.FORMER_NAME) for name in entry.former_names)
                forms.extend((name, MatchKind.ALIAS) for name in entry.also_known_as)
            compiled: list[SurfaceForm] = []
            for text, kind in forms:
                if _is_droppable_single_token(text):
                    continue
                pattern = _compile_form(text)
                if pattern is None:
                    continue
                compiled.append(SurfaceForm(isin=isin, text=text, kind=kind, pattern=pattern))
            candidate[isin] = compiled

        resolved = cls._drop_ambiguous(candidate)
        return cls(resolved)

    @staticmethod
    def _drop_ambiguous(
        candidate: Mapping[str, list[SurfaceForm]],
    ) -> dict[str, tuple[SurfaceForm, ...]]:
        """Drop any surface form whose normalized text is claimed by more than one held ISIN.

        An ambiguous name is never a pick (the identity master's rule): if "Larsen & Toubro" would
        resolve to two held ISINs, it links to neither and the log records the collision so a
        curator can disambiguate. Forms unique to one ISIN survive unchanged.
        """
        owners: dict[str, set[str]] = {}
        for isin, forms in candidate.items():
            for form in forms:
                owners.setdefault(normalize(form.text), set()).add(isin)
        ambiguous = {text for text, isins in owners.items() if len(isins) > 1}
        if ambiguous:
            _LOG.warning("linkage.ambiguous_forms_dropped", forms=sorted(ambiguous))
        return {
            isin: tuple(form for form in forms if normalize(form.text) not in ambiguous)
            for isin, forms in candidate.items()
        }

    def isins(self) -> frozenset[str]:
        """The held ISINs this resolver can link to."""
        return frozenset(self._forms_by_isin)

    def link(self, row: NewsRow) -> tuple[NewsLink, ...]:
        """The held ISINs a single news row is about, each with the form that fired.

        Matches every surface form against the row's headline and tagged entities in one normalized
        haystack. At most one link per ISIN (the first form that fires), because a holding is linked
        or it is not — a second matching form adds no information and would double-count in the
        escalation. Returns links in ISIN order for reproducibility.
        """
        haystack = _haystack(row)
        links: list[NewsLink] = []
        for isin in sorted(self._forms_by_isin):
            for form in self._forms_by_isin[isin]:
                if form.pattern.search(haystack):
                    links.append(
                        NewsLink(news_row=row, isin=isin, matched_form=form.text, kind=form.kind)
                    )
                    break
        return tuple(links)

    def link_all(self, rows: Iterable[NewsRow]) -> tuple[NewsLink, ...]:
        """Link a stream of news rows — every (row, held-ISIN) pair the resolver finds."""
        return tuple(link for row in rows for link in self.link(row))


def _haystack(row: NewsRow) -> str:
    """The one normalized string a row is matched against: its headline plus its tagged entities.

    A GDELT row has no headline but does tag actors (`entities`); an RSS row has a headline but no
    entities. Joining both and normalizing once means a name is found wherever the source stated it,
    and neither source shape needs a branch here.
    """
    parts: list[str] = []
    if row.title is not None:
        parts.append(row.title)
    parts.extend(row.entities)
    return normalize(" ".join(parts))
