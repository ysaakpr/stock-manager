"""The environment a `claude` CLI subprocess is given: an allowlist, never the parent's whole env.

The CLI is a third-party program that talks to the network and, for the Commons fetcher, browses
the open web. The process that launches it holds `DATABASE_URL`, broker keys and every other
setting the platform runs on; inheriting them would hand all of that to a child that needs none of
it, and a crash dump or a debug log on its side would carry them somewhere this repo cannot mask
(invariant #13). So the child gets what it needs to find itself, its config and its own credential
— which it keeps under `HOME`, in a file this module never opens — plus locale and network
plumbing, and nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

__all__ = ["CLAUDE_CLI_ENV_ALLOWLIST", "claude_cli_env"]

#: Names passed through when the parent has them. `PATH` finds the executable (and Node, for an npm
#: install); `HOME`, `CLAUDE_CONFIG_DIR` and the XDG directories are where the CLI keeps its config
#: and the credential `claude setup-token` / `claude login` wrote; `USER`/`LOGNAME` answer its
#: user lookups; locale and `TZ` keep its output decodable and its timestamps sane; `TMPDIR` is
#: where it writes scratch; the proxy and CA settings are how it reaches the API on a host that
#: needs them. Nothing here is a `*_KEY`, `*_TOKEN` or `*_SECRET`: an env-var credential for the
#: CLI (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`) is deliberately not forwarded — the
#: subscription login under `HOME` is how this platform authenticates it.
CLAUDE_CLI_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)


def claude_cli_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env for one `claude` subprocess: the allowlisted names `environ` has, and no others.

    What it does: copies each `CLAUDE_CLI_ENV_ALLOWLIST` name present in `environ` (the process
    environment by default).
    What it assumes: the CLI authenticates from its own credential store under `HOME`.
    What it never does: forward a name off the list, read any value's meaning, or add a default
    the parent did not have.
    """
    source = os.environ if environ is None else environ
    return {name: source[name] for name in CLAUDE_CLI_ENV_ALLOWLIST if name in source}
