"""SSRF guard: only let agent-driven fetches reach public internet hosts.

Every address a hostname resolves to must be globally routable; loopback, private, link-local (cloud metadata),
multicast, reserved and unspecified ranges are refused, including when smuggled via IPv4-mapped / 6to4 / Teredo /
NAT64 IPv6; the deprecated IPv4-compatible (``::a.b.c.d``) and SIIT (``::ffff:0:a.b.c.d``) forms are refused outright.

DNS rebinding: checking a name and then letting the HTTP client resolve it again leaves a window in which the name
can flip to a private address. `resolve_public` therefore resolves once and returns the validated IP, and
`pinned_get` dials exactly that IP: the request URL carries the IP literal, the ``Host`` header carries the original
host, and the ``sni_hostname`` request extension makes httpcore send the original host as TLS SNI *and* as the
``server_hostname`` the default SSL context verifies the certificate against - so TLS verification stays on and is
checked against the name the caller asked for, not the IP. Proxies from the environment are ignored
(``trust_env=False``), since a proxy would resolve the name itself.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx


class UnsafeURL(ValueError):
    """The URL must not be fetched; str(exc) is a short reason."""


IP = ipaddress.IPv4Address | ipaddress.IPv6Address

_V4_COMPAT = ipaddress.ip_network("::/96")  # ::a.b.c.d (RFC 4291 deprecated); :: and ::1 are handled as such
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")  # ::ffff:0:a.b.c.d (RFC 2765 SIIT)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_v4(ip: IP) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses tunnelled inside an IPv6 address (mapped, 6to4, Teredo, NAT64, IPv4-compatible, SIIT)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    out = [a for a in (ip.ipv4_mapped, ip.sixtofour) if a is not None]
    if ip.teredo:
        out.extend(ip.teredo)
    if ip in _NAT64 or ip in _SIIT or (ip in _V4_COMPAT and int(ip) > 1):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def ip_problem(ip: IP) -> str | None:
    """Why this address is not public, or None if it is. Tunnelled IPv4 is checked first so the reason names it."""
    embedded = _embedded_v4(ip)
    for a in [*embedded, ip]:
        for attr in ("loopback", "private", "link_local", "multicast", "reserved", "unspecified"):
            if getattr(a, f"is_{attr}"):
                return f"{attr.replace('_', '-')} address {a}"
        if not a.is_global:
            return f"non-public address {a}"
    if isinstance(ip, ipaddress.IPv6Address) and embedded and (ip in _SIIT or ip in _V4_COMPAT):
        return f"deprecated IPv4-in-IPv6 address {ip}"
    return None


@dataclass(frozen=True)
class Target:
    """A URL whose host has been resolved once and validated; `ip` is the address the request must dial."""

    url: str
    host: str
    ip: IP

    @property
    def pinned_url(self) -> str:
        """`url` with the host replaced by the validated IP literal (port, path and query kept)."""
        parts = urlsplit(self.url)
        ip = f"[{self.ip}]" if self.ip.version == 6 else str(self.ip)
        return urlunsplit(parts._replace(netloc=ip + (f":{parts.port}" if parts.port else "")))

    @property
    def ascii_host(self) -> str:
        """The host as sent on the wire (IDNA-encoded), used for the Host header and TLS SNI/verification."""
        return httpx.URL(self.url).raw_host.decode("ascii")

    @property
    def host_header(self) -> str:
        parts = urlsplit(self.url)
        host = f"[{self.ascii_host}]" if ":" in self.ascii_host else self.ascii_host
        return host + (f":{parts.port}" if parts.port else "")


def check_public_url(url: str) -> str:
    """Raise UnsafeURL unless `url` is http(s), has no userinfo and every resolved address is public. Returns the host."""
    return resolve_public(url).host


def resolve_public(url: str) -> Target:
    """Resolve `url`'s host exactly once; raise UnsafeURL unless it is http(s), has no userinfo and EVERY resolved
    address is public. Returns the first resolved address, to be dialled by `pinned_get` (never re-resolve)."""
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
    return Target(url=url, host=host, ip=addrs[0])


def pinned_get(target: Target, *, timeout: float = 15, headers: dict[str, str] | None = None) -> httpx.Response:
    """GET `target` without following redirects, connecting to the validated `target.ip` only (see module doc)."""
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        request = client.build_request("GET", target.pinned_url, headers={**(headers or {}), "Host": target.host_header},
                                       extensions={"sni_hostname": target.ascii_host})
        return client.send(request)
