"""SSRF guard for fetch_url: DNS and HTTP are monkeypatched, nothing leaves the process."""

import socket

import httpx
import pytest

from aecom import net_guard
from aecom.tools import catalog

DNS = {"example.com": ["93.184.216.34"], "localhost": ["127.0.0.1", "::1"], "intranet.corp": ["10.1.2.3"],
       "router.lan": ["192.168.1.1"], "metadata.evil": ["169.254.169.254"],
       "mixed.evil": ["93.184.216.34", "10.0.0.5"], "mapped.evil": ["::ffff:127.0.0.1"]}


@pytest.fixture
def net(monkeypatch):
    """Fake resolver + fake httpx.get; returns the list of URLs actually requested."""
    def getaddrinfo(host, port, *a, **kw):
        if host not in DNS:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in DNS[host]]

    requested: list[str] = []
    routes: dict[str, httpx.Response] = {}

    def get(url, **kw):
        assert kw.get("follow_redirects") is False
        requested.append(url)
        return routes.get(url) or httpx.Response(200, text="<html><body>Hello public web</body></html>")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(catalog.httpx, "get", get)
    return requested, routes


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/health", "http://localhost/", "http://10.0.0.1/", "http://intranet.corp/",
    "http://192.168.1.1/admin", "http://router.lan/", "http://169.254.169.254/latest/meta-data/",
    "http://metadata.evil/", "http://[::1]:8000/", "http://[::ffff:127.0.0.1]/", "http://mapped.evil/",
    "http://mixed.evil/", "http://0.0.0.0/", "http://user:pw@example.com/", "file:///etc/passwd", "http:///nohost",
])
def test_fetch_url_refuses_non_public(net, url):
    requested, _ = net
    out = catalog.fetch_url(url)
    assert out.startswith("refused:"), out
    assert requested == []


def test_fetch_url_allows_public_host(net):
    requested, _ = net
    out = catalog.fetch_url("https://example.com/page")
    assert "Hello public web" in out and "<untrusted" in out
    assert requested == ["https://example.com/page"]


def test_redirect_to_private_is_refused(net):
    requested, routes = net
    routes["https://example.com/r"] = httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
    out = catalog.fetch_url("https://example.com/r")
    assert out.startswith("refused:") and "169.254.169.254" in out
    assert requested == ["https://example.com/r"]


def test_redirects_are_followed_and_capped(net):
    requested, routes = net
    routes["https://example.com/a"] = httpx.Response(301, headers={"Location": "/b"})
    assert "Hello public web" in catalog.fetch_url("https://example.com/a")
    assert requested == ["https://example.com/a", "https://example.com/b"]

    requested.clear()
    routes["https://example.com/loop"] = httpx.Response(302, headers={"Location": "/loop"})
    assert catalog.fetch_url("https://example.com/loop") == "refused: too many redirects"
    assert len(requested) == 6


def test_ip_problem_classifies_tunnelled_addresses():
    import ipaddress

    assert net_guard.ip_problem(ipaddress.ip_address("8.8.8.8")) is None
    for bad in ["2002:7f00:1::", "64:ff9b::a9fe:a9fe", "fe80::1", "ff02::1", "100.64.0.1"]:
        assert net_guard.ip_problem(ipaddress.ip_address(bad)), bad
