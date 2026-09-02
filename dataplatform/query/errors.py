"""D4 query errors — shared so the query modules can raise without importing each other's bodies.

`QueryError` lived in `service.py` through M4.1. M4.2 adds `screen.py`, whose `QuarantineError`
subclasses it, and `service.py` in turn wires the screen — a cycle if the error stayed in either
body. Extracting it here breaks the cycle: every query module imports the error from this leaf,
and `service.QueryError` remains importable (re-exported there) so nothing downstream moves.
"""

from __future__ import annotations

__all__ = ["QueryError"]


class QueryError(Exception):
    """A query could not be answered from the lake — a missing primary, an incomplete L1, etc.

    Distinct from the identity layer's `IdentityError`: this is the query service telling its caller
    the *request* cannot be served against the data on disk, not that an identity rule was violated.
    """
