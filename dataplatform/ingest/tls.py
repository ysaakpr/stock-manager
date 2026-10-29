"""D1: per-host extra chain certificates — the one sanctioned adjustment to TLS on the fetch path.

Some servers present a valid certificate but omit the intermediates that link it to a root.
Browsers fill the gap by downloading the missing issuer from the leaf's AIA URL; OpenSSL, and so
httpx, does not, and the fetch dies with `CERTIFICATE_VERIFY_FAILED`. The answer is never to relax
verification. It is to hand OpenSSL the public certificates the server forgot, for that host only,
and let it verify exactly as before: full chain to a root already in the store, hostname checked.

A host opts in through its `tls_extra_chain` record in `source_register.yaml`, which names a PEM
under `dataplatform/ingest/` and pins every certificate in it by SHA-256. This module turns that
record into an httpx mount for `https://<host>` and nothing wider.

**Why the pins sit in that host's CA bundle.** Python's `ssl` gives a context one certificate
store and no separate "untrusted intermediates" input (OpenSSL's `-untrusted`), so the pinned
certificates are loaded into the mount's store next to the default roots. They still cannot act as
trust anchors, for two reasons this module enforces rather than assumes:

* none of them is self-signed — `read_extra_chain` refuses a PEM holding one, so a root CA can never
  be added this way; and
* `VERIFY_X509_PARTIAL_CHAIN` is cleared on the context. Without it OpenSSL accepts a chain only
  when it ends at a *self-signed* certificate in the store — a root from the default bundle — so a
  pinned intermediate can extend a chain but never terminate one.

What it never does: disable verification, turn off hostname checking, set partial chains, add a
self-signed certificate, or apply a pin to any host but the one whose register record names it.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import httpx

from dataplatform.clock import Clock
from dataplatform.ingest.source_register import SourceRegister, TlsExtraChain
from dataplatform.logging import get_logger

__all__ = [
    "CERTS_ROOT",
    "ExtraChain",
    "ExtraChainError",
    "extra_chain_context",
    "extra_chain_mounts",
    "load_extra_chains",
    "read_extra_chain",
    "warn_if_review_overdue",
]

_LOG = get_logger(__name__)

#: The directory `TlsExtraChain.pem` paths are relative to.
CERTS_ROOT: Final[Path] = Path(__file__).parent

_PEM_BLOCK: Final = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----"
)

#: DER tags walked to reach a certificate's issuer and subject.
_SEQUENCE: Final = 0x30
_EXPLICIT_VERSION: Final = 0xA0


class ExtraChainError(RuntimeError):
    """A pinned chain that does not match its register record, or a context that would not verify.

    Raised when a fetcher is built, never swallowed: a pin that cannot be trusted is a fetcher that
    must not start, not a host that quietly falls back to some other trust decision.
    """


@dataclass(frozen=True, slots=True)
class ExtraChain:
    """One host's pinned chain certificates, read from disk and checked against the register."""

    host: str
    pem_blocks: tuple[str, ...]
    sha256: tuple[str, ...]
    anchor: str
    review_by: date

    @property
    def mount_pattern(self) -> str:
        """The httpx mount key: HTTPS to exactly this host (any port), no subdomain, no wildcard."""
        return f"https://{self.host}"

    @property
    def cadata(self) -> str:
        """The certificates as one PEM string, comments stripped — what the context loads."""
        return "\n".join(self.pem_blocks) + "\n"


def read_extra_chain(host: str, record: TlsExtraChain, *, root: Path = CERTS_ROOT) -> ExtraChain:
    """Read `record.pem` and prove it holds exactly the pinned certificates, none of them a root.

    What it assumes: the register record was reviewed when it was committed; this checks the file
    still matches it. What it never does: accept a certificate whose fingerprint is not pinned, an
    extra or missing certificate, a reordering, or a self-signed certificate.
    """
    path = root / record.pem
    text = path.read_text(encoding="ascii")
    blocks = tuple(match.group(0) for match in _PEM_BLOCK.finditer(text))
    ders = [ssl.PEM_cert_to_DER_cert(block) for block in blocks]
    found = [hashlib.sha256(der).hexdigest() for der in ders]
    if found != record.sha256:
        raise ExtraChainError(
            f"{host}: {path} holds certificates {found}, but the register pins "
            f"{record.sha256}; refusing to use a chain nobody reviewed"
        )
    for fingerprint, der in zip(found, ders, strict=True):
        if _is_self_signed(der):
            raise ExtraChainError(
                f"{host}: certificate {fingerprint} in {path} is self-signed — a root CA. Extra "
                "chain certificates only help OpenSSL reach a root already in the store; adding a "
                "root is never done here"
            )
    return ExtraChain(
        host=host,
        pem_blocks=blocks,
        sha256=tuple(found),
        anchor=record.anchor,
        review_by=record.review_by,
    )


def load_extra_chains(
    register: SourceRegister, *, root: Path = CERTS_ROOT
) -> tuple[ExtraChain, ...]:
    """Every host's pinned chain in the register, each read and checked (`read_extra_chain`)."""
    return tuple(
        read_extra_chain(policy.host, policy.tls_extra_chain, root=root)
        for policy in register.hosts
        if policy.tls_extra_chain is not None
    )


def extra_chain_context(
    chain: ExtraChain,
    *,
    base: Callable[[], ssl.SSLContext] = httpx.create_ssl_context,
) -> ssl.SSLContext:
    """httpx's own verifying context plus `chain`'s certificates, with partial chains cleared.

    `base` defaults to `httpx.create_ssl_context`, which is exactly what the client's default
    transport builds (certifi, or `SSL_CERT_FILE`/`SSL_CERT_DIR` when set), so the pinned host is
    anchored to the same roots as every other host. Raises `ExtraChainError` if the base context
    does not require a verified certificate and a matching hostname — a pin is only ever added to a
    context that verifies, never used to paper over one that does not.
    """
    context = base()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ExtraChainError(
            f"{chain.host}: the base TLS context does not verify (verify_mode="
            f"{context.verify_mode!r}, check_hostname={context.check_hostname}); refusing to pin "
            "extra certificates onto it"
        )
    context.load_verify_locations(cadata=chain.cadata)
    # Python 3.13+ sets this by default; cleared explicitly so no interpreter upgrade can turn a
    # pinned intermediate into a trust anchor.
    context.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


def extra_chain_mounts(chains: Sequence[ExtraChain]) -> Mapping[str, httpx.BaseTransport]:
    """One httpx transport per pinned host, keyed by `https://<host>` and by nothing broader."""
    return {
        chain.mount_pattern: httpx.HTTPTransport(verify=extra_chain_context(chain))
        for chain in chains
    }


def warn_if_review_overdue(chains: Sequence[ExtraChain], *, clock: Clock) -> None:
    """Log a warning for each pin past its `review_by` date — the pin's built-in expiry notice.

    A pin outliving its reason is how a workaround becomes permanent. It keeps working (the pinned
    certificates themselves are valid for years); the warning says it is due for removal or
    re-verification, every time a fetcher is built.
    """
    today = clock.today()
    for chain in chains:
        if today > chain.review_by:
            _LOG.warning(
                "fetch.tls_extra_chain_review_overdue",
                host=chain.host,
                review_by=chain.review_by.isoformat(),
                today=today.isoformat(),
                anchor=chain.anchor,
                action=(
                    "remove the pin if certifi now carries the issuer's root or the site serves "
                    "its own chain; otherwise re-verify the certificates and extend review_by"
                ),
            )


def _is_self_signed(der: bytes) -> bool:
    """True when a certificate's issuer and subject Names are byte-identical — a root.

    Walks just enough DER (RFC 5280: Certificate → TBSCertificate → [version], serial, signature,
    issuer, validity, subject) to compare the two Names, so the check needs no third-party parser.
    """
    _, cert_start, _ = _tlv(der, 0, expect=_SEQUENCE)
    _, tbs_start, tbs_end = _tlv(der, cert_start, expect=_SEQUENCE)
    fields: list[bytes] = []
    pos = tbs_start
    while pos < tbs_end and len(fields) < 6:
        tag, _, end = _tlv(der, pos)
        if not (tag == _EXPLICIT_VERSION and not fields):
            fields.append(der[pos:end])
        pos = end
    if len(fields) < 5:
        raise ExtraChainError("certificate DER is truncated before its subject Name")
    _serial, _signature, issuer, _validity, subject = fields[:5]
    return issuer == subject


def _tlv(der: bytes, pos: int, *, expect: int | None = None) -> tuple[int, int, int]:
    """(tag, content start, content end) of the DER element at `pos`; loud on anything malformed."""
    if pos + 2 > len(der):
        raise ExtraChainError("certificate DER is truncated")
    tag, length = der[pos], der[pos + 1]
    pos += 2
    if expect is not None and tag != expect:
        raise ExtraChainError(f"certificate DER: expected tag {expect:#x}, found {tag:#x}")
    if length & 0x80:
        width = length & 0x7F
        if not 1 <= width <= 4 or pos + width > len(der):
            raise ExtraChainError("certificate DER has an unsupported length encoding")
        length = int.from_bytes(der[pos : pos + width], "big")
        pos += width
    if pos + length > len(der):
        raise ExtraChainError("certificate DER element runs past the end of the certificate")
    return tag, pos, pos + length
