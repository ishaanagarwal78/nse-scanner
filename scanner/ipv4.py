"""Make this process connect over IPv4 only (imported by scanner/__init__.py and the Pi scripts).

Why: home ISPs such as Jio and Airtel give each connection its own public IPv6 address, while one IPv4 address is
shared by many customers (carrier-grade NAT). By default most requests go out over IPv6, which points at this
one home connection. Over IPv4 the sites we read (NSE, BSE, GeM, Vahan, ...) see a shared address instead.

Covers Python's own lookups (requests, yfinance, websockets, asyncio) and curl_cffi, which resolves names itself.
If a host has no IPv4 address at all, the normal lookup is used so nothing breaks. FORCE_IPV4=0 turns it off.
"""
import os
import socket

_orig = socket.getaddrinfo


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family in (0, socket.AF_UNSPEC):
        try:
            res = _orig(host, port, socket.AF_INET, type, proto, flags)
            if res:
                return res
        except socket.gaierror:
            pass   # IPv6-only host: fall back
    return _orig(host, port, family, type, proto, flags)


def enable():
    if os.environ.get("FORCE_IPV4", "1") == "0" or getattr(socket, "_ipv4_only", False):
        return
    socket.getaddrinfo = _getaddrinfo
    socket._ipv4_only = True
    try:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession, Session
        for cls in (Session, AsyncSession):
            init = cls.__init__

            def wrapped(self, *a, _init=init, **kw):
                opts = dict(kw.pop("curl_options", None) or {})
                opts.setdefault(CurlOpt.IPRESOLVE, 1)   # CURL_IPRESOLVE_V4
                _init(self, *a, curl_options=opts, **kw)
            cls.__init__ = wrapped
    except Exception:
        pass


enable()
