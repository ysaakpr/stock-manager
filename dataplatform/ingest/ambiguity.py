"""D2 at ingest: what a writer does when the identity master names two ISINs for one symbol.

`IdentityMaster.try_resolve` raises `AmbiguousSymbolError` — and queues the conflict for a human —
when two of its windows name different ISINs for one `(exchange, symbol, date)`. That is the right
answer for the master, which must never pick. It was the wrong answer for a *file*: a writer that
let it propagate failed every row of the session for one symbol (`nse_delivery`, CCAVENUE,
2026-10-26 .. 28, PR #141). The rule #141 set for delivery, lifted here so the deals and F&O
writers apply the same one rather than a copy of it:

1. **The session settles it, when it can.** If the same session's own exchange file states exactly
   one of the master's candidate ISINs for the symbol, the row is placed there and counted. Not a
   tie-break: the exchange named the instrument for that session, and the candidate set only bounds
   what that statement may pick. A statement outside the candidates, or naming several of them, is
   not used.
2. **Otherwise the row is quarantined**, with its candidates, and raised as an ERROR quality flag
   (`ambiguity_finding`) so it reaches `/status/quality` — never guessed onto an ISIN, never
   dropped. The master conflict stays queued either way; nothing here edits the master.
3. **Past `MAX_AMBIGUOUS_QUARANTINED` the file fails** (`refuse_over_threshold`): that many is a
   broken master, and publishing around a broken master would hide it.

What it never does: join on a symbol (invariant #2) — every ISIN it returns is one the master
already holds for that symbol that day, chosen by the session's own statement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from dataplatform.identity.master import AmbiguousSymbolError, Exchange, IdentityError
from dataplatform.logging import get_logger

if TYPE_CHECKING:  # imported at call time: `quality` imports `ingest.nse`, whose writers use this
    from dataplatform.quality.sentinel import QualityFinding

__all__ = [
    "MAX_AMBIGUOUS_QUARANTINED",
    "Ambiguity",
    "AmbiguousFileError",
    "ambiguity_finding",
    "ambiguity_of",
    "refuse_over_threshold",
]

_LOG = get_logger(__name__)

#: The most distinct ambiguous keys one file may quarantine (the master names two ISINs and the
#: session cannot settle it) before the whole file fails instead.
#:
#: Why 5, and why a count rather than a share of the file. One ISIN reissue makes one symbol
#: ambiguous, and in practice the session's own statement settles it before it gets here
#: (CCAVENUE, 2026-10-26 .. 28: one delivery row a day, all settled). The identity history has
#: exactly two shapes: a single reissue (CCAVENUE, reconciliation 72/73, 2026-10-24) and a broken
#: refresh that left *seven* NSE symbols ambiguous at once (ids 21-27, 2026-10-10). Five admits a
#: reissue with room for a second, and refuses the broken-refresh shape. A broken master scales
#: with the number of symbols it mangles, not with the file's length, so the bar is absolute. A
#: delivery file counts `(symbol, series)` rows (a symbol occupies two or three); a deals file and
#: an F&O file count symbols, because one symbol there can carry many rows (a dozen clients in one
#: bulk deal) and one reissue is still one defect.
MAX_AMBIGUOUS_QUARANTINED: Final = 5


class AmbiguousFileError(IdentityError):
    """More rows were ambiguous than one ISIN reissue explains — the file fails.

    Raised by `refuse_over_threshold`. An `IdentityError`, so a caller that already treats identity
    failures as a failed session keeps doing so; each writer raises its own subclass so the error
    names the file kind (`delivery.AmbiguousDeliveryFileError`, ...).
    """


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """What the master said about a symbol (`candidates`) and what the session stated (`stated`).

    Both sorted. `stated` is every ISIN the session's own exchange file named for the symbol — for
    a `(symbol, series)` lookup at most one, for a symbol-only lookup (a deals or F&O file, which
    name no series) one per series the symbol traded in. Empty when the session said nothing.
    """

    candidates: tuple[str, ...]
    stated: tuple[str, ...] = ()

    @property
    def settled(self) -> str | None:
        """The one candidate the session stated, or `None` when it stated none or several."""
        named = [isin for isin in self.stated if isin in self.candidates]
        return named[0] if len(named) == 1 else None

    @property
    def reason(self) -> str:
        """Why the session did not settle it, in the words the quality flag carries."""
        head = (
            "ambiguous: the identity master names several ISINs for this symbol on this date and "
            "the session's bhavcopy states "
        )
        named = [isin for isin in self.stated if isin in self.candidates]
        if not self.stated:
            return head + "none for it"
        if not named:
            return head + f"{', '.join(self.stated)}, which is not one of them"
        if len(named) > 1:
            return head + f"more than one of them ({', '.join(named)})"
        return head + f"{named[0]}, which settles it"


def ambiguity_of(error: AmbiguousSymbolError, stated: Iterable[str]) -> Ambiguity:
    """The master's candidates from `error`, against what the session stated."""
    return Ambiguity(candidates=error.conflict.isins, stated=tuple(sorted(set(stated))))


def refuse_over_threshold(
    held: Sequence[str],
    *,
    error: type[AmbiguousFileError],
    what: str,
    event: str,
    source: str,
    exchange: Exchange,
    threshold: int = MAX_AMBIGUOUS_QUARANTINED,
) -> None:
    """Raise `error` when `held` names more distinct ambiguous keys than `threshold`.

    `held` is one short description per quarantined row (`SYMBOL/SERIES->[...]` or `SYMBOL->[...]`);
    duplicates collapse, so a symbol with many rows counts once when the caller keys by symbol.
    `what` names the rows in the message (`"delivery rows"`, `"deals symbols"`); `event` prefixes
    the structured log line (`<event>.ambiguity_over_threshold`).
    What it never does: raise at or under the threshold — that is the case it exists to let through.
    """
    distinct = sorted(set(held))
    if len(distinct) <= threshold:
        return
    _LOG.error(
        f"{event}.ambiguity_over_threshold",
        source=source,
        exchange=exchange.value,
        ambiguous=len(distinct),
        threshold=threshold,
        sample=distinct[:10],
        state="FAILED",
    )
    raise error(
        f"{len(distinct)} {what} are ambiguous in the identity master and unsettled by the "
        f"session's bhavcopy, more than the {threshold} one ISIN reissue explains — a broken "
        f"master, so the file is refused rather than published around it: "
        f"{', '.join(distinct[:10])}"
    )


def ambiguity_finding(
    *,
    check_name: str,
    source: str,
    exchange: Exchange,
    symbol: str,
    logical_date: date,
    ambiguity: Ambiguity,
    key: str,
    detail: Mapping[str, object],
) -> QualityFinding:
    """One ERROR finding for one quarantined ambiguity — what `/status/quality` shows.

    What it does: carries the symbol, the session, the candidate ISINs, what the session stated and
    why that did not settle it, plus the writer's own `detail` (the row's facts), under
    `check_name`. ERROR because it is a real identity defect, not noise. Fingerprinted by
    `(check_name, exchange, key, session)`, so a re-run of the same file finds the open flag
    instead of stacking another; `key` is what makes a row distinct within the file.
    What it assumes: `source` is the dataset the row was held back from, never a source an
    interlock judges — the point of quarantining one row is that it does not unpublish the rest.
    What it never does: name an ISIN in the flag's `isin` column — there is no one ISIN to name.
    """
    from dataplatform.quality.sentinel import QualityFinding, finding_fingerprint

    return QualityFinding(
        logical_date=logical_date,
        check_name=check_name,
        severity="ERROR",
        source=source,
        detail={
            "exchange": exchange.value,
            "symbol": symbol,
            "trade_date": logical_date.isoformat(),
            "candidates": list(ambiguity.candidates),
            "reason": ambiguity.reason,
            **detail,
        },
        fingerprint=finding_fingerprint(f"{check_name}/{exchange.value}/{key}", None, logical_date),
    )
