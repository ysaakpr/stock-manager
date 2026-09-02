"""D7 sentinel rules — one anomaly class per file, each self-registering on import.

This package exists so the sentinel's premise holds literally: "sentinel rules grow every time
something surprises", and growing them must never touch the engine. A rule is a new file here that
calls `dataplatform.quality.sentinel.register`; `sentinel._discover` imports every module in this
package (`pkgutil.iter_modules` over `__path__`), so a dropped-in file is picked up with no edit to
any manifest, this `__init__`, or the engine.

Import order does not matter — `run_sentinel` sorts rules by name — so this file deliberately does
*not* import the submodules itself. Discovery does, lazily and once, which keeps a bare
`import dataplatform.quality.rules` free of side effects until the engine actually asks for the set.
"""

from __future__ import annotations
