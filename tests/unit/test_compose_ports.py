"""Every port the compose stack publishes binds to loopback, and only loopback (M15.6).

Docker publishes a bare `"5433:5432"` on 0.0.0.0 *and* [::], and its own iptables rules sit
ahead of the host's INPUT chain, so a host firewall does not close it. Until M15.6 that put the
Postgres (dev-default credentials) and the unauthenticated status API on every interface. These
tests parse the compose files and fail on a publish with no host IP, a wildcard host IP, a
non-loopback host IP, or a host IP taken from a variable — an override is one `.env` line away
from 0.0.0.0, so the address is a literal on purpose — and on `network_mode: host`, which puts
every listening port on the host's interfaces without any `ports:` entry to check.

The parser is exercised on bad specs too, so a parser that stopped finding the host IP cannot
pass the real file vacuously.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
# Pruned, not filtered afterwards: the main checkout's data/ is the whole lake.
_NOT_SOURCE = {".git", ".venv", "node_modules", "data", ".worktrees", ".claude", ".polly"}


def _compose_files() -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _NOT_SOURCE]
        found += [
            Path(root, f)
            for f in files
            if fnmatch(f, "docker-compose*.y*ml") or fnmatch(f, "compose*.y*ml")
        ]
    return sorted(found)


COMPOSE_FILES = _compose_files()

# `${VAR}`, `${VAR:-default}` and friends. Replaced before splitting so a `:-` inside a default
# is not mistaken for the ip:published:target separator.
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_VAR = "\x00var\x00"


@dataclass(frozen=True)
class Publish:
    service: str
    spec: str
    host_ip: str | None  # None: no host IP given, which docker reads as every interface


def _host_ip_of_short(spec: str) -> str | None:
    """The host IP of a short-syntax port, `[ip:][published:]target[/proto]`, or None."""
    s = _INTERPOLATION.sub(_VAR, spec).split("/", 1)[0]
    if s.startswith("["):
        return s[1 : s.index("]")]
    parts = s.split(":")
    if len(parts) <= 2:
        return None
    # An unbracketed IPv6 host (`::1:5433:5432`) leaves its own colons in the head.
    return ":".join(parts[:-2])


def published_ports(compose: dict[str, Any]) -> list[Publish]:
    """Every `ports:` entry of every service, with its host IP as written (None if absent)."""
    out: list[Publish] = []
    for name, service in (compose.get("services") or {}).items():
        for entry in service.get("ports") or []:
            if isinstance(entry, dict):
                ip = entry.get("host_ip")
                host_ip = None if ip is None else _INTERPOLATION.sub(_VAR, str(ip))
                out.append(Publish(name, repr(entry), host_ip))
            else:
                out.append(Publish(name, str(entry), _host_ip_of_short(str(entry))))
    return out


def host_networked(compose: dict[str, Any]) -> list[str]:
    """Services on `network_mode: host`: every port they listen on is on the host's interfaces,
    with no `ports:` entry for the checks above to see."""
    return [
        name
        for name, service in (compose.get("services") or {}).items()
        if str(service.get("network_mode", "")).strip() == "host"
    ]


def violation(p: Publish) -> str | None:
    """Why this publish is not loopback-only, or None if it is."""
    if p.host_ip is None or p.host_ip == "":
        return "no host IP: docker binds 0.0.0.0 and [::]"
    if _VAR in p.host_ip:
        return "host IP comes from a variable; it must be a loopback literal"
    try:
        ip = ipaddress.ip_address(p.host_ip)
    except ValueError:
        return f"host IP {p.host_ip!r} is not an IP address"
    if not ip.is_loopback:
        return f"host IP {p.host_ip} is not loopback"
    return None


def test_the_repo_has_a_compose_file() -> None:
    assert REPO / "ops" / "docker-compose.yml" in COMPOSE_FILES


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_every_published_port_binds_loopback_only(path: Path) -> None:
    compose = yaml.safe_load(path.read_text())
    ports = published_ports(compose)
    bad = [f"{p.service}: {p.spec!r} — {why}" for p in ports if (why := violation(p))]
    bad += [f"{name}: network_mode: host bypasses the publish" for name in host_networked(compose)]
    assert not bad, "\n".join(bad)


def test_both_services_are_still_published() -> None:
    """The fix is a bind address, not dropping the publish: host tooling needs both ports."""
    compose = yaml.safe_load((REPO / "ops" / "docker-compose.yml").read_text())
    by_service = {p.service: p.host_ip for p in published_ports(compose)}
    assert by_service == {"postgres": "127.0.0.1", "app": "127.0.0.1"}


@pytest.mark.parametrize(
    "spec",
    [
        "5433:5432",
        "${POSTGRES_HOST_PORT:-5433}:5432",
        "0.0.0.0:5433:5432",
        "0.0.0.0:${APP_HOST_PORT:-8000}:8000",
        "[::]:5433:5432",
        ":::5433:5432",
        "10.0.0.5:8000:8000",
        "${BIND_IP:-127.0.0.1}:8000:8000",
        "8000",
        "8000/tcp",
    ],
)
def test_a_non_loopback_short_spec_is_caught(spec: str) -> None:
    compose = {"services": {"svc": {"ports": [spec]}}}
    [publish] = published_ports(compose)
    assert violation(publish) is not None


@pytest.mark.parametrize(
    "entry",
    [
        {"target": 5432, "published": "5433"},
        {"target": 5432, "published": "5433", "host_ip": "0.0.0.0"},
        {"target": 5432, "published": "5433", "host_ip": "::"},
    ],
)
def test_a_non_loopback_long_spec_is_caught(entry: dict[str, Any]) -> None:
    [publish] = published_ports({"services": {"svc": {"ports": [entry]}}})
    assert violation(publish) is not None


def test_a_long_spec_host_ip_from_a_variable_is_reported_as_such() -> None:
    entry = {"target": 8000, "published": "8000", "host_ip": "${BIND_IP:-127.0.0.1}"}
    [publish] = published_ports({"services": {"svc": {"ports": [entry]}}})
    assert violation(publish) == "host IP comes from a variable; it must be a loopback literal"


def test_host_network_mode_is_caught() -> None:
    compose = {"services": {"app": {"network_mode": "host"}, "db": {"network_mode": "bridge"}}}
    assert host_networked(compose) == ["app"]


@pytest.mark.parametrize(
    "spec",
    [
        "127.0.0.1:5433:5432",
        "127.0.0.1:${POSTGRES_HOST_PORT:-5433}:5432",
        "127.0.0.1:8000:8000/tcp",
        "[::1]:5433:5432",
    ],
)
def test_a_loopback_spec_passes(spec: str) -> None:
    [publish] = published_ports({"services": {"svc": {"ports": [spec]}}})
    assert violation(publish) is None
