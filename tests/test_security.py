"""SSRF guard for fetch_url: DNS and HTTP are monkeypatched, nothing leaves the process."""

import socket

import httpx
import pytest

from aecom import net_guard
from aecom.tools import catalog

DNS = {"example.com": ["93.184.216.34"], "localhost": ["127.0.0.1", "::1"], "intranet.corp": ["10.1.2.3"],
       "router.lan": ["192.168.1.1"], "metadata.evil": ["169.254.169.254"],
       "mixed.evil": ["93.184.216.34", "10.0.0.5"], "mapped.evil": ["::ffff:127.0.0.1"]}


class _Net(tuple):
    """(requested, routes) for unpacking, plus `.dialled`: the httpx.Requests as handed to the transport."""

    def __new__(cls, requested, routes, dialled):
        self = super().__new__(cls, (requested, routes))
        self.dialled = dialled
        return self


@pytest.fixture
def net(monkeypatch):
    """Fake resolver + fake httpx transport send; returns (requested logical URLs, routes, dialled requests)."""
    def getaddrinfo(host, port, *a, **kw):
        if host not in DNS:
            raise socket.gaierror("unknown host")
        ips = DNS[host]
        if callable(ips):
            ips = ips()
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    requested: list[str] = []
    routes: dict[str, httpx.Response] = {}
    dialled: list[httpx.Request] = []

    def send(self, request, **kw):
        assert self.follow_redirects is False and kw.get("follow_redirects") in (None, False)
        dialled.append(request)
        url = f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"
        requested.append(url)
        return routes.get(url) or httpx.Response(200, text="<html><body>Hello public web</body></html>")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(httpx.Client, "send", send)
    return _Net(requested, routes, dialled)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/health", "http://localhost/", "http://10.0.0.1/", "http://intranet.corp/",
    "http://192.168.1.1/admin", "http://router.lan/", "http://169.254.169.254/latest/meta-data/",
    "http://metadata.evil/", "http://[::1]:8000/", "http://[::ffff:127.0.0.1]/", "http://mapped.evil/",
    "http://mixed.evil/", "http://0.0.0.0/", "http://[::127.0.0.1]/", "http://[::ffff:0:7f00:1]/",
    "http://user:pw@example.com/", "file:///etc/passwd", "http:///nohost",
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
    (req,) = net.dialled
    assert req.url.host == "93.184.216.34" and req.extensions["sni_hostname"] == "example.com"


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
    for bad in ["::127.0.0.1", "::ffff:0:7f00:1", "::10.0.0.1"]:
        assert "address" in (net_guard.ip_problem(ipaddress.ip_address(bad)) or ""), bad


@pytest.mark.parametrize("addr,v4", [("::127.0.0.1", "127.0.0.1"), ("::ffff:0:7f00:1", "127.0.0.1"),
                                     ("::ffff:0:a00:1", "10.0.0.1")])
def test_ipv4_compatible_and_siit_name_the_embedded_address(addr, v4):
    import ipaddress

    problem = net_guard.ip_problem(ipaddress.ip_address(addr))
    assert problem and v4 in problem, problem
    # even a public IPv4 is refused in these deprecated forms
    assert net_guard.ip_problem(ipaddress.ip_address("::ffff:0:808:808"))


def test_dns_rebinding_cannot_redirect_the_connection(net, monkeypatch):
    """The name resolves public on the check and private afterwards: the request still dials the checked IP."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]])
    calls: list[int] = []

    def rebinding():
        calls.append(1)
        return next(answers)

    monkeypatch.setitem(DNS, "rebind.evil", rebinding)
    requested, _ = net
    out = catalog.fetch_url("https://rebind.evil/x")
    assert "Hello public web" in out
    assert len(calls) == 1  # resolved exactly once; the HTTP client never re-resolves
    (req,) = net.dialled
    assert req.url.host == "93.184.216.34" and req.url.port is None
    assert req.headers["host"] == "rebind.evil"
    assert req.extensions["sni_hostname"] == "rebind.evil"  # TLS SNI + certificate check use the real name
    assert requested == ["https://rebind.evil/x"]


def test_pinned_request_keeps_port_and_ipv6_brackets(net, monkeypatch):
    monkeypatch.setitem(DNS, "v6.example", ["2606:2800:220:1:248:1893:25c8:1946"])
    catalog.fetch_url("http://v6.example:8080/p?q=1")
    (req,) = net.dialled
    assert str(req.url) == "http://[2606:2800:220:1:248:1893:25c8:1946]:8080/p?q=1"
    assert req.headers["host"] == "v6.example:8080"


def test_redirect_without_location_stops_after_one_request(net):
    requested, routes = net
    routes["https://example.com/noloc"] = httpx.Response(302)
    out = catalog.fetch_url("https://example.com/noloc")
    assert out == "refused: redirect (302) without Location header"
    assert requested == ["https://example.com/noloc"]
