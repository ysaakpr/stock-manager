"""Pinned extra chain certificates fix eaindustry.nic.in without weakening TLS (D1, M11.1).

On 2026-10-28 eaindustry.nic.in renewed to a Let's Encrypt leaf issued by YR1 and began serving the
leaf alone; the WPI capture failed `CERTIFICATE_VERIFY_FAILED` the next day. The fix pins YR1 and
ISRG Root X1's cross-signature of Root YR for that one host. Five properties carry it, each asserted
here rather than assumed:

1. **It works.** The frozen leaf the server really sent verifies through the mount the client
   routes `https://eaindustry.nic.in` to, ending at ISRG Root X1 — a root certifi already carries.
2. **It is needed.** Through the default transport, without the pins, the same leaf fails.
3. **It is scoped.** Only `https://eaindustry.nic.in` is mounted; `www.`, look-alike hosts, other
   hosts and plain HTTP all go through the untouched default transport. One register host, only.
4. **The pins are never anchors.** `VERIFY_X509_PARTIAL_CHAIN` stays cleared — the pins alone
   verify nothing — and neither pin may be self-signed.
5. **Verification stays on.** Certificate and hostname checks are required; a base context that
   does not verify is refused.

The real chain cannot be handshaken offline (its key is the site's), so (1)-(2) run `openssl
verify` against the exact CA material and flags of the context httpx would use, at a frozen time
inside the leaf's validity. A synthetic PKI generated per run (keys in a temp dir, never
committed) then proves the same properties through a real in-memory Python TLS handshake.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
import ssl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import certifi
import httpx
import pytest
from structlog.testing import capture_logs

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import FrozenClock
from dataplatform.ingest.fetcher import HttpxTransport, build_fetcher
from dataplatform.ingest.source_register import SourceRegister, TlsExtraChain
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.tls import (
    ExtraChain,
    ExtraChainError,
    extra_chain_context,
    extra_chain_mounts,
    load_extra_chains,
    read_extra_chain,
    warn_if_review_overdue,
)
from dataplatform.store.l0 import L0Store

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "tls"
LEAF: Final = FIXTURES / "eaindustry.nic.in" / "2026-10-28" / "leaf.pem"

HOST: Final = "eaindustry.nic.in"
WPI_PAGE: Final = "https://eaindustry.nic.in/download_data_2223.asp"
YR1: Final = "13949634d99cd6fd6aa80bc034fefacceb1969feef986586713ecdbb05758d3f"
ROOT_YR_BY_X1: Final = "072639d0b140d5bffae16ad9c3f6cc6086040621f51ee61a6d46a8915c07cf76"

#: 2026-10-29 12:00 UTC: inside the leaf's validity (2026-10-28 .. 2027-01-26), frozen so the
#: verification below does not start failing when the real leaf expires.
VERIFY_AT: Final = int(datetime(2026, 10, 29, 12, tzinfo=UTC).timestamp())

#: URLs the pinned mount must serve — the host itself, with and without its port.
PINNED_URLS: Final = (
    WPI_PAGE,
    "https://eaindustry.nic.in/indx_download_2223/wpi_monthly_index_202609.xlsx",
    "https://eaindustry.nic.in:443/robots.txt",
)

#: URLs that must never see the pins: a SAN sibling, look-alikes, other register hosts, plain HTTP.
UNPINNED_URLS: Final = (
    "https://www.eaindustry.nic.in/",
    "https://eaindustry.nic.in.example.org/",
    "https://xeaindustry.nic.in/",
    "https://nic.in/",
    "https://nsearchives.nseindia.com/content/cm/x.csv",
    "https://www.nseindia.com/",
    "https://tutorial.gst.gov.in/",
    "http://eaindustry.nic.in/download_data_2223.asp",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: the handshakes below run over memory BIOs (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("TLS tests must never touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="module")
def register() -> SourceRegister:
    """The real, checked-in Source Register."""
    return load_register()


@pytest.fixture(scope="module")
def chains(register: SourceRegister) -> tuple[ExtraChain, ...]:
    """The register's pinned chains, read and fingerprint-checked by the production loader."""
    return load_extra_chains(register)


@pytest.fixture
def pinned(chains: tuple[ExtraChain, ...]) -> HttpxTransport:
    """The production transport, built with the register's pins exactly as `build_fetcher` does."""
    return HttpxTransport(extra_chains=chains)


@pytest.fixture
def bare() -> HttpxTransport:
    """The transport as it was before the fix: no pins."""
    return HttpxTransport()


def openssl() -> str:
    """The openssl binary. Absent is a failure, not a skip: these tests are the fix's evidence."""
    path = shutil.which("openssl")
    if path is None:
        pytest.fail("openssl is not on PATH; the chain-verification tests need it")
    return path


def context_for(transport: HttpxTransport, url: str) -> ssl.SSLContext:
    """The SSL context the client would verify `url`'s server with.

    Reads httpx's own routing (`_transport_for_url`) and the context its connection pool holds
    — private attributes of the pinned httpx (uv.lock), used so this checks what the client really
    does rather than a re-implementation of it. A refactor upstream fails here loudly.
    """
    routed = transport._client._transport_for_url(httpx.URL(url))
    assert isinstance(routed, httpx.HTTPTransport)
    context = routed._pool._ssl_context
    assert isinstance(context, ssl.SSLContext)
    return context


def ca_fingerprints(context: ssl.SSLContext) -> set[str]:
    """SHA-256 of every CA certificate loaded into `context`."""
    return {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True)}


def openssl_verify(
    context: ssl.SSLContext,
    tmp_path: Path,
    *,
    host: str = HOST,
    ca_pem: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Verify the frozen leaf the way `context` would: its CA material, flags and hostname rule.

    Fails the test outright if `context` does not verify at all — verification disabled is never a
    configuration this suite accepts. `ca_pem` replaces the context's CA material (to ask what the
    pins alone would anchor) while keeping its flags.
    """
    assert context.verify_mode == ssl.CERT_REQUIRED, "certificate verification is disabled"
    assert context.check_hostname, "hostname checking is disabled"
    if ca_pem is None:
        ca_pem = "".join(
            ssl.DER_cert_to_PEM_cert(der) for der in context.get_ca_certs(binary_form=True)
        )
    cafile = tmp_path / "ca.pem"
    cafile.write_text(ca_pem, encoding="ascii")
    args = [
        openssl(),
        "verify",
        "-no-CApath",
        "-no-CAstore",
        "-CAfile",
        str(cafile),
        "-attime",
        str(VERIFY_AT),
        "-verify_hostname",
        host,
        "-show_chain",
    ]
    if context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN:
        args.append("-partial_chain")
    if context.verify_flags & ssl.VERIFY_X509_STRICT:
        args.append("-x509_strict")
    args.append(str(LEAF))
    return subprocess.run(args, capture_output=True, text=True, check=False)


# ── the register record ─────────────────────────────────────────────────────────────────────────


def test_the_register_pins_two_certificates_for_one_host_only(register: SourceRegister) -> None:
    pinned_hosts = [h.host for h in register.hosts if h.tls_extra_chain is not None]
    assert pinned_hosts == [HOST], "an extra chain is scoped to eaindustry.nic.in and nothing else"
    chains = load_extra_chains(register)
    assert [(c.host, c.sha256, c.anchor) for c in chains] == [
        (HOST, (YR1, ROOT_YR_BY_X1), "ISRG Root X1")
    ]


def test_the_mount_is_the_one_host_over_https(chains: tuple[ExtraChain, ...]) -> None:
    assert set(extra_chain_mounts(chains)) == {"https://eaindustry.nic.in"}


# ── 1-2: the real chain, with and without the pins ──────────────────────────────────────────────


def test_the_frozen_leaf_verifies_through_the_mount_to_isrg_root_x1(
    pinned: HttpxTransport, tmp_path: Path
) -> None:
    result = openssl_verify(context_for(pinned, WPI_PAGE), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    chain = [line for line in result.stdout.splitlines() if line.startswith("depth=")]
    assert len(chain) == 4, result.stdout
    assert "CN=YR1" in chain[1] and "CN=Root YR" in chain[2]
    # The chain ends at a self-signed root from certifi, not at a pin.
    assert chain[3].endswith("CN=ISRG Root X1"), result.stdout


def test_without_the_pins_the_frozen_leaf_fails(bare: HttpxTransport, tmp_path: Path) -> None:
    result = openssl_verify(context_for(bare, WPI_PAGE), tmp_path)
    assert result.returncode != 0
    assert "unable to get local issuer certificate" in result.stdout + result.stderr


# ── 3: scope ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", PINNED_URLS)
def test_the_pinned_host_gets_the_pins(pinned: HttpxTransport, url: str) -> None:
    assert {YR1, ROOT_YR_BY_X1} <= ca_fingerprints(context_for(pinned, url))


@pytest.mark.parametrize("url", UNPINNED_URLS)
def test_other_hosts_never_get_the_pins(pinned: HttpxTransport, url: str) -> None:
    routed = pinned._client._transport_for_url(httpx.URL(url))
    assert routed is pinned._client._transport, f"{url} was routed to a mount"
    assert not {YR1, ROOT_YR_BY_X1} & ca_fingerprints(context_for(pinned, url))


def test_a_caller_built_client_cannot_silently_drop_the_pins(
    chains: tuple[ExtraChain, ...],
) -> None:
    with pytest.raises(ValueError, match="not both"):
        HttpxTransport(client=httpx.Client(), extra_chains=chains)


def test_build_fetcher_wires_the_pins_into_the_real_transport(tmp_path: Path) -> None:
    clock = FrozenClock(date(2026, 10, 29))
    fetcher = build_fetcher(
        clock=clock, alerter=NullAlerter(), l0=L0Store(clock=clock, data_root=tmp_path)
    )
    transport = fetcher._transport
    assert isinstance(transport, HttpxTransport)
    assert {YR1, ROOT_YR_BY_X1} <= ca_fingerprints(context_for(transport, WPI_PAGE))
    assert not {YR1, ROOT_YR_BY_X1} & ca_fingerprints(
        context_for(transport, "https://nsearchives.nseindia.com/")
    )


# ── 4: the pins are never anchors ──────────────────────────────────────────────────────────────


def test_partial_chain_is_off_and_the_pins_alone_anchor_nothing(
    pinned: HttpxTransport, chains: tuple[ExtraChain, ...], tmp_path: Path
) -> None:
    context = context_for(pinned, WPI_PAGE)
    assert not context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    # Under the context's own flags, the pins without certifi's roots must verify nothing. With
    # partial chains on, this verifies — which is exactly the anchor the design forbids.
    result = openssl_verify(context, tmp_path, ca_pem=chains[0].cadata)
    assert result.returncode != 0, "the pinned certificates alone anchored the chain"


def test_partial_chain_on_the_base_context_is_cleared(chains: tuple[ExtraChain, ...]) -> None:
    def base() -> ssl.SSLContext:  # what Python 3.13+'s create_default_context sets
        context = httpx.create_ssl_context()
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context

    context = extra_chain_context(chains[0], base=base)
    assert not context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def test_a_self_signed_certificate_is_never_pinned(tmp_path: Path) -> None:
    root_x1 = _certifi_pem("ISRG Root X1")
    (tmp_path / "root.pem").write_text(root_x1, encoding="ascii")
    record = _record("root.pem", [_fingerprint(root_x1)])
    with pytest.raises(ExtraChainError, match="self-signed"):
        read_extra_chain(HOST, record, root=tmp_path)


@pytest.mark.parametrize(
    "pins",
    [[YR1], [ROOT_YR_BY_X1, YR1], [YR1, ROOT_YR_BY_X1, YR1], [YR1, "0" * 64]],
    ids=["missing", "reordered", "extra", "changed"],
)
def test_a_pem_that_does_not_match_its_pins_is_refused(
    register: SourceRegister, pins: list[str]
) -> None:
    host = register.host_policy(HOST)
    assert host is not None and host.tls_extra_chain is not None
    record = host.tls_extra_chain.model_copy(update={"sha256": pins})
    with pytest.raises(ExtraChainError, match="refusing to use a chain nobody reviewed"):
        read_extra_chain(HOST, record)


# ── 5: verification stays on ────────────────────────────────────────────────────────────────────


def test_the_mount_requires_a_verified_certificate_and_hostname(pinned: HttpxTransport) -> None:
    context = context_for(pinned, WPI_PAGE)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_the_mount_still_checks_the_hostname(pinned: HttpxTransport, tmp_path: Path) -> None:
    result = openssl_verify(context_for(pinned, WPI_PAGE), tmp_path, host="www.nseindia.com")
    assert result.returncode != 0
    assert "hostname mismatch" in result.stdout + result.stderr


@pytest.mark.parametrize("verify_mode", [ssl.CERT_REQUIRED, ssl.CERT_NONE])
def test_a_base_context_that_does_not_verify_is_refused(
    chains: tuple[ExtraChain, ...], verify_mode: ssl.VerifyMode
) -> None:
    def base() -> ssl.SSLContext:
        context = httpx.create_ssl_context()
        context.check_hostname = False
        context.verify_mode = verify_mode
        return context

    with pytest.raises(ExtraChainError, match="does not verify"):
        extra_chain_context(chains[0], base=base)


# ── review_by and the in-use log ────────────────────────────────────────────────────────────────


def test_a_pin_past_review_by_warns(chains: tuple[ExtraChain, ...]) -> None:
    assert chains[0].review_by == date(2027, 1, 26)
    with capture_logs() as on_the_day:
        warn_if_review_overdue(chains, clock=FrozenClock(date(2027, 1, 26)))
    with capture_logs() as day_after:
        warn_if_review_overdue(chains, clock=FrozenClock(date(2027, 1, 27)))
    assert on_the_day == []
    assert [(e["event"], e["log_level"], e["host"]) for e in day_after] == [
        ("fetch.tls_extra_chain_review_overdue", "warning", HOST)
    ]


def test_the_first_request_to_the_pinned_host_is_logged_once(
    pinned: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, content=b"ok", request=httpx.Request(method, url))

    monkeypatch.setattr(pinned._client, "request", answer)
    with capture_logs() as entries:
        for url in (WPI_PAGE, WPI_PAGE, "https://www.nseindia.com/", PINNED_URLS[1]):
            pinned.request("GET", url, headers={}, timeout=5.0)
    used = [e for e in entries if e["event"] == "fetch.tls_extra_chain"]
    assert [(e["host"], e["certificates"]) for e in used] == [(HOST, [YR1, ROOT_YR_BY_X1])]


# ── the same properties through a real Python TLS handshake (synthetic PKI) ────────────────────


@dataclass(frozen=True, slots=True)
class SyntheticPki:
    """A root, an intermediate and a leaf for `pinned.test`, generated per run in a temp dir."""

    root: Path
    intermediate: Path
    leaf: Path
    leaf_key: Path


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> SyntheticPki:
    """Mirror the real failure: a server whose leaf needs an intermediate it does not send.

    Keys are generated into a temp dir for the life of the run and never committed.
    """
    base = tmp_path_factory.mktemp("pki")
    tool = openssl()

    def run(*args: str) -> None:
        subprocess.run([tool, *args], cwd=base, check=True, capture_output=True)

    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    run(
        "req",
        "-x509",
        *ec,
        "-keyout",
        "root.key",
        "-out",
        "root.pem",
        "-subj",
        "/CN=Test Root",
        "-days",
        "30",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    (base / "int.ext").write_text(
        "basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n",
        encoding="ascii",
    )
    run("req", "-new", *ec, "-keyout", "int.key", "-out", "int.csr", "-subj", "/CN=Test Int")
    run(
        "x509",
        "-req",
        "-in",
        "int.csr",
        "-CA",
        "root.pem",
        "-CAkey",
        "root.key",
        "-set_serial",
        "2",
        "-days",
        "30",
        "-extfile",
        "int.ext",
        "-out",
        "int.pem",
    )
    (base / "leaf.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:pinned.test\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n",
        encoding="ascii",
    )
    run("req", "-new", *ec, "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=pinned.test")
    run(
        "x509",
        "-req",
        "-in",
        "leaf.csr",
        "-CA",
        "int.pem",
        "-CAkey",
        "int.key",
        "-set_serial",
        "3",
        "-days",
        "30",
        "-extfile",
        "leaf.ext",
        "-out",
        "leaf.pem",
    )
    return SyntheticPki(
        root=base / "root.pem",
        intermediate=base / "int.pem",
        leaf=base / "leaf.pem",
        leaf_key=base / "leaf.key",
    )


def handshake(client_context: ssl.SSLContext, pki: SyntheticPki, hostname: str) -> None:
    """One TLS handshake over memory BIOs; the server sends its leaf and nothing else."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(pki.leaf, pki.leaf_key)
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        client_in.write(server_out.read())
        if client_done and server_done:
            return
    raise AssertionError("the in-memory handshake did not complete")


def synthetic_chain(pki: SyntheticPki) -> ExtraChain:
    """The synthetic intermediate, read through the production loader."""
    pem = pki.intermediate.read_text(encoding="ascii")
    return read_extra_chain(
        "pinned.test", _record(pki.intermediate.name, [_fingerprint(pem)]), root=pki.root.parent
    )


def with_root(pki: SyntheticPki) -> Callable[[], ssl.SSLContext]:
    """A verifying base context whose only root is the synthetic one (certifi's stand-in)."""
    return lambda: ssl.create_default_context(cafile=pki.root)


def test_handshake_succeeds_with_the_pinned_intermediate(pki: SyntheticPki) -> None:
    handshake(extra_chain_context(synthetic_chain(pki), base=with_root(pki)), pki, "pinned.test")


def test_handshake_fails_without_the_pinned_intermediate(pki: SyntheticPki) -> None:
    with pytest.raises(ssl.SSLCertVerificationError, match="unable to get local issuer"):
        handshake(with_root(pki)(), pki, "pinned.test")


def test_handshake_fails_when_the_pin_would_have_to_be_the_anchor(pki: SyntheticPki) -> None:
    def no_roots() -> ssl.SSLContext:  # verifying, but with no root to reach
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    context = extra_chain_context(synthetic_chain(pki), base=no_roots)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, pki, "pinned.test")
    # The flag this module clears is the whole difference: set it and the pin becomes an anchor.
    context = extra_chain_context(synthetic_chain(pki), base=no_roots)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    handshake(context, pki, "pinned.test")


def test_handshake_fails_on_a_hostname_the_leaf_does_not_name(pki: SyntheticPki) -> None:
    context = extra_chain_context(synthetic_chain(pki), base=with_root(pki))
    with pytest.raises(ssl.SSLCertVerificationError, match="Hostname mismatch"):
        handshake(context, pki, "other.test")


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────


class NullAlerter:
    """Satisfies the `Alerter` protocol; nothing in these tests should alert."""

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        raise AssertionError(f"unexpected alert: {title}")


def _record(pem: str, sha256: list[str]) -> TlsExtraChain:
    return TlsExtraChain(
        pem=pem, sha256=sha256, anchor="test", review_by=date(2027, 1, 26), reason="test"
    )


def _fingerprint(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def _certifi_pem(label: str) -> str:
    """One root's PEM block out of certifi's bundle, by its label comment."""
    text = Path(certifi.where()).read_text(encoding="utf-8")
    start = text.index(f'# Label: "{label}"')
    begin = text.index("-----BEGIN CERTIFICATE-----", start)
    end = text.index("-----END CERTIFICATE-----", begin) + len("-----END CERTIFICATE-----")
    return text[begin:end] + "\n"
