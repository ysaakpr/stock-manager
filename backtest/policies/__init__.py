"""X2 backtest policies — strategy objects the replay engine drives (EXECUTION_PLAN §7).

A policy is the one thing the replay engine (:mod:`backtest.replay`) does not implement: the
decision. Each policy here satisfies :class:`backtest.replay.Policy` — it looks at a session's
point-in-time context and returns what to do — so the *same* object runs under replay and (M5)
live, which is what makes "one decision code path" (invariant #5) a structural fact.

:class:`~backtest.policies.naive_momentum.NaiveMomentumPolicy` is M4.10's deliberately simple
engine-validation strategy: top-N trailing-return momentum from the PIT universe, rebalanced
monthly. Its purpose is to prove the engine — PIT universe, the shared cost model, whole-share
allocation, accounting and journaling — survives a full-history run, not to make money.
"""

from backtest.policies.naive_momentum import (
    MomentumData,
    MomentumParameters,
    MomentumRecord,
    NaiveMomentumPolicy,
)

__all__ = [
    "MomentumData",
    "MomentumParameters",
    "MomentumRecord",
    "NaiveMomentumPolicy",
]
