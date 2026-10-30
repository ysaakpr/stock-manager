"""The one socket of this package, behind a protocol so the test suite never opens one (B8).

`HttpxClient` is the network: one request, redirects *not* followed (the caller re-checks every
hop for SSRF and follows it itself), the body streamed and cut at `max_bytes` so a 2 GB response
costs 2 MB, and the connected peer's address handed to a check before a byte of body is read.
`RecordedHttpClient` answers from a script and records what it was asked, the same way
`dataplatform.ingest.fetcher.RecordedTransport` does for ingestion.

What this module never does: retry, sleep, rotate a user agent, use a proxy, or put a credential
anywhere but the header the caller passed. A credential is only ever a header value, so it is in
no URL, no argv and no exception message raised here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict

__all__ = [
    "HttpClient",
    "HttpReply",
    "HttpxClient",
    "PeerCheck",
    "RecordedCall",
    "RecordedHttpClient",
    "WebTimeoutError",
    "WebTransportError",
]

#: Called with the connected peer's IP (or None when the transport cannot say) before the body is
#: read. Raises to abort the response.
PeerCheck = Callable[[str | None], None]


class WebTransportError(RuntimeError):
    """No response was produced: connection refused, reset, TLS failure, DNS failure."""


class WebTimeoutError(WebTransportError):
    """The request did not complete within its timeout."""


class HttpReply(BaseModel):
    """One response: status, headers (lower-cased names), the body up to the cap, and the peer."""

    model_config = ConfigDict(frozen=True)

    status: int
    url: str
    headers: dict[str, str]
    body: bytes
    truncated: bool = False
    """True when the body was longer than `max_bytes` and the rest was never read."""
    peer_ip: str | None = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json_body(self) -> Any:
        """The body parsed as JSON. Raises ValueError on anything else."""
        return json.loads(self.body.decode("utf-8"))


class HttpClient(Protocol):
    """One request, no redirects followed, the body capped."""

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
        json_body: Mapping[str, Any] | None = None,
        check_peer: PeerCheck | None = None,
    ) -> HttpReply:
        """Perform one request. Raises `WebTransportError` (or `WebTimeoutError`) when no
        response was produced; a 4xx or 5xx is a response and is returned."""
        ...


class HttpxClient:
    """The real network, on one long-lived `httpx.Client` with redirects off."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client if client is not None else httpx.Client(follow_redirects=False)

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
        json_body: Mapping[str, Any] | None = None,
        check_peer: PeerCheck | None = None,
    ) -> HttpReply:
        try:
            with self._client.stream(
                method,
                url,
                headers=dict(headers),
                json=dict(json_body) if json_body is not None else None,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                peer = _peer_ip(response)
                if check_peer is not None:
                    check_peer(peer)
                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in response.iter_bytes():
                    room = max_bytes - size
                    if len(chunk) > room:
                        chunks.append(chunk[:room])
                        truncated = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                return HttpReply(
                    status=response.status_code,
                    url=str(response.url),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=b"".join(chunks),
                    truncated=truncated,
                    peer_ip=peer,
                )
        except httpx.TimeoutException as exc:
            raise WebTimeoutError(f"{method} {_host(url)} timed out after {timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise WebTransportError(f"{method} {_host(url)}: {type(exc).__name__}") from exc

    def close(self) -> None:
        self._client.close()


def _peer_ip(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    addr = stream.get_extra_info("server_addr")
    if isinstance(addr, tuple) and addr and isinstance(addr[0], str):
        return addr[0]
    return None


def _host(url: str) -> str:
    """Only the host goes into an error: a query string is the one part that could carry data."""
    return httpx.URL(url).host or "?"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """A request a `RecordedHttpClient` was asked to make."""

    method: str
    url: str
    headers: Mapping[str, str]
    json_body: Mapping[str, Any] | None


#: One scripted outcome: a reply, or an exception raised instead of answering.
Scripted = HttpReply | Exception


@dataclass
class RecordedHttpClient:
    """The offline client: replies come from a script keyed by URL, never from a socket.

    A sequence is consumed in order and its last entry repeats. A URL nobody scripted raises
    `AssertionError`, because code reaching an unexpected URL is the bug this exists to catch.
    `peers` maps a URL to the peer IP to report to `check_peer`, to exercise DNS rebinding.
    """

    script: Mapping[str, Scripted | Sequence[Scripted]]
    peers: Mapping[str, str] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)
    _cursor: dict[str, int] = field(default_factory=dict)

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
        json_body: Mapping[str, Any] | None = None,
        check_peer: PeerCheck | None = None,
    ) -> HttpReply:
        self.calls.append(RecordedCall(method, url, dict(headers), json_body))
        if url not in self.script:
            raise AssertionError(f"no scripted reply for {method} {url}")
        entry = self.script[url]
        outcomes: Sequence[Scripted] = (
            [entry] if isinstance(entry, HttpReply | Exception) else list(entry)
        )
        index = min(self._cursor.get(url, 0), len(outcomes) - 1)
        self._cursor[url] = index + 1
        outcome = outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        peer = self.peers.get(url, outcome.peer_ip)
        if check_peer is not None:
            check_peer(peer)
        body = outcome.body
        truncated = len(body) > max_bytes
        return outcome.model_copy(
            update={"url": url, "body": body[:max_bytes], "truncated": truncated, "peer_ip": peer}
        )

    def urls(self) -> list[str]:
        return [call.url for call in self.calls]
