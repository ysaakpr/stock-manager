"""What the fetch tool may reach: public http(s) pages, never our network or a scheduler host.

A model chooses the URL, and a page can tell a model which URL to choose, so every URL this package
fetches is hostile until `UrlGuard` has passed it. The checks, in order:

1. **Scheme** http or https; no userinfo; port 80, 443 or the scheme default. Anything else
   (`file:`, `gopher:`, `http://host:6379/`) is refused before a name is resolved.
2. **Scheduler-owned hosts** (`WEBSEARCH_BLOCKED_HOSTS`): the host or any subdomain of a listed
   host is refused. NSE, BSE and niftyindices have their own leases and request budgets
   (`dataplatform.ingest.lease`), and BSE is paused (D29), so a model-driven fetch must not spend
   them. Search results may still *name* these hosts; only fetching them is refused.
3. **Resolution**: the host is resolved, and *every* address it resolves to must be globally
   routable. Refused: loopback, RFC 1918 private, link-local 169.254.0.0/16 (the EC2 instance
   metadata service at 169.254.169.254 lives there), CGNAT 100.64.0.0/10, multicast, unspecified,
   reserved and documentation ranges, and IPv6 loopback, link-local fe80::/10, unique-local
   fc00::/7 and site-local fec0::/10. An IPv4 address embedded in IPv6 (mapped, NAT64, 6to4) is
   judged as the IPv4 address it carries. Numeric spellings such as `2130706433` resolve to what
   they mean and are judged on that.
4. **The connected peer** is checked again before the body is read (`check_peer`), which closes
   the DNS-rebinding window between step 3 and the connect.

Redirects are not followed by the client: the direct fetcher runs every hop through `check`
again and stops after `WEBSEARCH_MAX_REDIRECTS`.

What this module never does: make a request, or allow a URL because it "looks" public.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "Resolver",
    "UrlGuard",
    "UrlRefusedError",
    "host_is_blocked",
    "ip_refusal",
    "system_resolver",
]

#: host -> every address it resolves to. Injected so the tests never touch DNS.
Resolver = Callable[[str], Sequence[str]]

_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})
_METADATA: Final = ipaddress.ip_address("169.254.169.254")
_NAMED_V4: Final[tuple[tuple[str, ipaddress.IPv4Network], ...]] = (
    ("loopback", ipaddress.IPv4Network("127.0.0.0/8")),
    ("private", ipaddress.IPv4Network("10.0.0.0/8")),
    ("private", ipaddress.IPv4Network("172.16.0.0/12")),
    ("private", ipaddress.IPv4Network("192.168.0.0/16")),
    ("link-local", ipaddress.IPv4Network("169.254.0.0/16")),
    ("cgnat", ipaddress.IPv4Network("100.64.0.0/10")),
    ("multicast", ipaddress.IPv4Network("224.0.0.0/4")),
    ("unspecified", ipaddress.IPv4Network("0.0.0.0/8")),
)
_NAMED_V6: Final[tuple[tuple[str, ipaddress.IPv6Network], ...]] = (
    ("loopback", ipaddress.IPv6Network("::1/128")),
    ("unspecified", ipaddress.IPv6Network("::/128")),
    ("link-local", ipaddress.IPv6Network("fe80::/10")),
    ("site-local", ipaddress.IPv6Network("fec0::/10")),
    ("unique-local", ipaddress.IPv6Network("fc00::/7")),
    ("multicast", ipaddress.IPv6Network("ff00::/8")),
)
_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")


class UrlRefusedError(ValueError):
    """The fetch tool will not fetch this URL. `reason` is safe to show the model and to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def system_resolver(host: str) -> list[str]:
    """Every address `host` resolves to, via the system resolver."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise UrlRefusedError(f"the host {host!r} does not resolve") from exc
    return sorted({str(info[4][0]) for info in infos})


def ip_refusal(text: str) -> str | None:
    """Why the fetch tool may not connect to `text`, or None when it is a public address."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return f"{text!r} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            inner = ip_refusal(str(embedded))
            return None if inner is None else f"{inner} (embedded in {ip})"
        if ip.teredo is not None:
            return f"{ip} is a Teredo tunnel address"
        for label, net6 in _NAMED_V6:
            if ip in net6:
                return f"{ip} is {label}"
    else:
        if ip == _METADATA:
            return f"{ip} is link-local (the cloud instance metadata service)"
        for label, net4 in _NAMED_V4:
            if ip in net4:
                return f"{ip} is {label}"
    if not ip.is_global:
        return f"{ip} is not globally routable"
    return None


def _normal_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def host_is_blocked(host: str, blocked: Iterable[str]) -> bool:
    """True when `host` is a listed host or a subdomain of one (dot-boundary match)."""
    name = _normal_host(host)
    for entry in blocked:
        root = _normal_host(entry)
        if root and (name == root or name.endswith("." + root)):
            return True
    return False


class UrlGuard:
    """The SSRF and scheduler-host gate every fetched URL passes, every redirect hop included."""

    __slots__ = ("_blocked", "_resolver")

    def __init__(self, blocked_hosts: Iterable[str], *, resolver: Resolver = system_resolver):
        self._blocked = tuple(_normal_host(h) for h in blocked_hosts if h.strip())
        self._resolver = resolver

    @property
    def blocked_hosts(self) -> tuple[str, ...]:
        return self._blocked

    def check_form(self, url: str) -> str:
        """Steps 1-2: scheme, userinfo, port and blocklist, without resolving. Returns the host."""
        try:
            parts = urlsplit(url.strip())
            port = parts.port
        except ValueError as exc:
            raise UrlRefusedError(f"not a valid URL: {exc}") from exc
        if parts.scheme.lower() not in {"http", "https"}:
            raise UrlRefusedError(f"only http and https are fetched, not {parts.scheme!r}")
        if parts.username is not None or parts.password is not None:
            raise UrlRefusedError("a URL carrying a user name or password is not fetched")
        host = _normal_host(parts.hostname or "")
        if not host:
            raise UrlRefusedError("the URL has no host")
        if port is not None and port not in _ALLOWED_PORTS:
            raise UrlRefusedError(f"port {port} is not fetched; only 80 and 443")
        if host_is_blocked(host, self._blocked):
            raise UrlRefusedError(
                f"{host} is owned by the data scheduler, which has its own request budget for it; "
                "the web fetch tool does not fetch it"
            )
        return host

    def check(self, url: str) -> None:
        """Steps 1-3. Raises `UrlRefusedError` naming the first rule the URL breaks."""
        host = self.check_form(url)
        addresses = self._resolver(host)
        if not addresses:
            raise UrlRefusedError(f"the host {host!r} does not resolve")
        for address in addresses:
            reason = ip_refusal(address)
            if reason is not None:
                raise UrlRefusedError(f"{host} resolves to a non-public address: {reason}")

    def check_peer(self, peer_ip: str | None) -> None:
        """Step 4, for `HttpClient.send(check_peer=...)`: refuse a connection to a private peer.

        An unknown peer (a transport that cannot say) passes: step 3 already judged the name.
        """
        if peer_ip is None:
            return
        reason = ip_refusal(peer_ip)
        if reason is not None:
            raise UrlRefusedError(f"the connection reached a non-public address: {reason}")
