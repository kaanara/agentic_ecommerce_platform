"""SSRF guard: only let agent-driven fetches reach public internet hosts.

Every address a hostname resolves to must be globally routable; loopback, private, link-local (cloud metadata),
multicast, reserved and unspecified ranges are refused, including when smuggled via IPv4-mapped / 6to4 / Teredo IPv6.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURL(ValueError):
    """The URL must not be fetched; str(exc) is a short reason."""


IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def _embedded_v4(ip: IP) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses tunnelled inside an IPv6 address (mapped, 6to4, Teredo, NAT64-compat)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    out = [a for a in (ip.ipv4_mapped, ip.sixtofour) if a is not None]
    if ip.teredo:
        out.extend(ip.teredo)
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def ip_problem(ip: IP) -> str | None:
    """Why this address is not public, or None if it is."""
    for a in [ip, *_embedded_v4(ip)]:
        for attr in ("loopback", "private", "link_local", "multicast", "reserved", "unspecified"):
            if getattr(a, f"is_{attr}"):
                return f"{attr.replace('_', '-')} address {a}"
        if not a.is_global:
            return f"non-public address {a}"
    return None


def check_public_url(url: str) -> str:
    """Raise UnsafeURL unless `url` is http(s), has no userinfo and every resolved address is public. Returns the host."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL(f"malformed URL ({exc})") from None
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("only http(s) URLs are allowed")
    if "@" in parts.netloc:
        raise UnsafeURL("credentials in URL are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("URL has no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addrs = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError) as exc:
            raise UnsafeURL(f"cannot resolve {host} ({exc})") from None
        # strip any IPv6 zone id ("fe80::1%eth0") before parsing
        addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
        if not addrs:
            raise UnsafeURL(f"cannot resolve {host}")
    for a in addrs:
        if problem := ip_problem(a):
            raise UnsafeURL(f"{host} resolves to {problem}")
    return host
