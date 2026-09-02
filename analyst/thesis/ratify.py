"""A4: the thesis ratification workflow and version history.

`models.py` says what a single thesis version *is* and how one version turns into the next
(`revise`) or becomes ratified (`ratified_with`). This module holds the *history* those operations
walk: every version of every core holding's thesis, keyed by (case_id, isin), append-only, so the
two questions the governance model asks stay answerable —

* **"Is there a ratified thesis for this holding right now?"** — which is what gates a core buy
  (`current_ratified`), and
* **"Which thesis backed that buy back then?"** — which needs every superseded version kept, not
  overwritten.

The book is the in-memory shape of what M5.8 will persist and M5.7 will assemble into a proposal.
It is deliberately storage-free and clock-free: a `Ratification` carries its own timestamp from an
injected `Clock` (B10), and the book only orders and retrieves. The one rule it enforces beyond the
model's own is the workflow rule — a version is proposed, then ratified, then (when its successor is
ratified) superseded, and no step may be skipped or repeated.

`Ratification` and `RatificationKind` are M5.3's governance artifact, reused unchanged: a thesis is
ratified by the same act as a policy set (§5.1), and `RatificationKind.FIXTURE` (B9) rules a thesis
paper-only exactly as it rules a policy set — the real-money guard lives in the funding path
(`analyst.cases.lifecycle`), not here.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from analyst.cases.policies import Ratification
from analyst.thesis.models import (
    Thesis,
    ThesisStatus,
    ThesisVersionError,
)

__all__ = ["ThesisBook", "ThesisKey", "UnknownThesisError"]


class UnknownThesisError(ThesisVersionError):
    """A thesis was asked for that the book has never seen for this (case, isin)."""


@dataclass(frozen=True, slots=True)
class ThesisKey:
    """What a thesis history is filed under: a case and a holding, joined on ISIN (#2), never a
    symbol.
    """

    case_id: str
    isin: str


@dataclass
class _History:
    """Every version of one holding's thesis, oldest first. Append-only in spirit."""

    versions: list[Thesis] = field(default_factory=list)


class ThesisBook:
    """The version history of every core holding's thesis, and the ratification workflow over it.

    What it does: records proposed versions, ratifies them (superseding the prior ratified version
    in the same step), revises ratified ones into fresh proposals, and answers whether a holding
    currently has a ratified thesis.
    What it assumes: one logical writer at a time — it is an in-memory model of a persisted history,
    not a concurrency primitive. Timestamps arrive on the `Ratification` from an injected `Clock`.
    What it never does: mutate a stored version in place or drop one. Ratifying replaces the
    *status*
    of the prior version by storing a new `SUPERSEDED` object; the original bytes are still
    reconstructable from the version list, and nothing is ever deleted (a thesis history is a
    governance record).
    """

    __slots__ = ("_by_key",)

    def __init__(self) -> None:
        self._by_key: dict[ThesisKey, _History] = {}

    # ── proposing ──

    def propose(self, thesis: Thesis) -> Thesis:
        """Record a first, un-ratified version (v1) for a holding that has none.

        A new proposal, not a revision: use `revise()` to draft the next version of a holding that
        already has a history. This refuses a thesis that is not v1, or one for a holding already in
        the book, so the two entry points cannot be confused.
        """
        if thesis.status is not ThesisStatus.PROPOSAL:
            raise ThesisVersionError(
                f"propose() takes a fresh {ThesisStatus.PROPOSAL.value} thesis; got "
                f"{thesis.status.value} (a ratified thesis is proposed via propose() then ratify())"
            )
        if thesis.version != 1:
            raise ThesisVersionError(
                f"propose() records the first version; version {thesis.version} is a revision — "
                "draft it from the ratified version with revise(), then propose_revision()"
            )
        key = ThesisKey(thesis.case_id, thesis.isin)
        if key in self._by_key:
            raise ThesisVersionError(
                f"{thesis.isin} in case {thesis.case_id} already has a thesis history; the next "
                "version is a revision, not a new proposal"
            )
        self._by_key[key] = _History(versions=[thesis])
        return thesis

    def propose_revision(self, revision: Thesis) -> Thesis:
        """Record a revised version (v2+) produced by `Thesis.revise()`, awaiting its own
        ratification.

        The revision must be the exact next version of the holding's current top version and sit in
        `PROPOSAL` — `revise()` guarantees both, and this checks them so a revision cannot be filed
        against the wrong parent.
        """
        if revision.status is not ThesisStatus.PROPOSAL:
            raise ThesisVersionError(
                f"a revision is recorded in {ThesisStatus.PROPOSAL.value}; "
                f"got {revision.status.value}"
            )
        history = self._require(ThesisKey(revision.case_id, revision.isin))
        top = history.versions[-1]
        if revision.version != top.version + 1:
            raise ThesisVersionError(
                f"revision is version {revision.version} but the history is at version "
                f"{top.version}; draft it from the current version with revise()"
            )
        history.versions.append(revision)
        return revision

    # ── ratifying ──

    def ratify(self, key: ThesisKey, ratification: Ratification) -> Thesis:
        """Ratify the holding's top (proposed) version, superseding the prior ratified one.

        What it does: turns the current proposal into `RATIFIED` (checking the ratification pins its
        content), and marks any previously ratified version `SUPERSEDED` — both stored, so the
        history reads as one ratified version at a time with a full trail behind it.
        What it never does: ratify a version that is not the top of the history, or re-ratify one —
        `Thesis.ratified_with` enforces the latter.
        """
        history = self._require(key)
        top = history.versions[-1]
        if top.status is not ThesisStatus.PROPOSAL:
            raise ThesisVersionError(
                f"the top version of {key.isin} in case {key.case_id} is {top.status.value}, not a "
                "proposal; there is nothing awaiting ratification"
            )
        ratified = top.ratified_with(ratification)
        # Retire the prior ratified version, if any, keeping its bytes (never delete a governance
        # record).
        for index, version in enumerate(history.versions):
            if version.status is ThesisStatus.RATIFIED:
                history.versions[index] = version.superseded()
        history.versions[-1] = ratified
        return ratified

    def revise(self, key: ThesisKey, **changes: object) -> Thesis:
        """Draft and record the next version of a holding's ratified thesis (§5.1: an edit is a new
        version).

        A convenience over `current_ratified().revise(...)` + `propose_revision(...)`: it revises
        the *ratified* version (not a dangling proposal), records the result in `PROPOSAL`, and
        returns it awaiting its own ratification. The ratified version is left untouched until the
        revision is itself ratified.
        """
        ratified = self.current_ratified(key)
        if ratified is None:
            raise ThesisVersionError(
                f"{key.isin} in case {key.case_id} has no ratified thesis to revise; "
                "propose one first"
            )
        return self.propose_revision(ratified.revise(**changes))

    # ── reading ──

    def current_ratified(self, key: ThesisKey) -> Thesis | None:
        """The holding's ratified thesis, or None if it has never had one (or only a proposal).

        This is what a core buy is gated on: `None` means no buy (see `analyst.thesis.engine`).
        """
        history = self._by_key.get(key)
        if history is None:
            return None
        for version in history.versions:
            if version.status is ThesisStatus.RATIFIED:
                return version
        return None

    def has_ratified_thesis(self, key: ThesisKey) -> bool:
        """Whether the holding has a ratified thesis — the core-buy gate as a predicate."""
        return self.current_ratified(key) is not None

    def versions(self, key: ThesisKey) -> Sequence[Thesis]:
        """Every version recorded for a holding, oldest first. The governance trail."""
        return tuple(self._require(key).versions)

    def keys(self) -> Iterator[ThesisKey]:
        """Every holding the book knows a thesis for."""
        return iter(self._by_key)

    def as_mapping(self) -> Mapping[ThesisKey, Sequence[Thesis]]:
        """A read-only snapshot of the whole book — what M5.7 folds into a proposal document."""
        return {key: tuple(history.versions) for key, history in self._by_key.items()}

    def _require(self, key: ThesisKey) -> _History:
        history = self._by_key.get(key)
        if history is None:
            raise UnknownThesisError(
                f"{key.isin} in case {key.case_id} has no thesis history; propose one first"
            )
        return history
