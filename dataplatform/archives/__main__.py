"""`python -m dataplatform.archives --date YYYY-MM-DD` — publish one date's bundle.

A thin package entry point so the CLI is invoked as `python -m dataplatform.archives` without the
double-import warning that `python -m dataplatform.archives.publisher` raises (the module is already
imported by the package `__init__`). The real work is `dataplatform.archives.publisher.main`.
"""

from __future__ import annotations

import sys

from dataplatform.archives.publisher import main

if __name__ == "__main__":
    sys.exit(main())
