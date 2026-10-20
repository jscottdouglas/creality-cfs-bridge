"""Finding the printer again after DHCP hands it a new address.

The K2 announces itself over mDNS as `<hostname>.local` (K2Plus-XXXX.local).
Windows resolves that natively; WSL does not, so from WSL the lookup is handed
to `powershell.exe Resolve-DnsName`. When neither answers, the /24 the printer
was last seen on is swept for a Moonraker whose `/printer/info` reports the
same hostname. Everything here is read-only: plain GETs and name lookups.
"""
from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests

log = logging.getLogger("cfsbridge.discover")

MOONRAKER_PORT = 7125
RESOLVE_TIMEOUT = 15.0
SCAN_TIMEOUT = 0.8
SCAN_WORKERS = 64


def _is_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def resolve_local(name: str) -> Optional[str]:
    """`<name>.local` to an IPv4 address, or None.

    The process's own resolver first (enough on Windows), then Windows' resolver
    through powershell.exe when running under WSL.
    """
    if not name:
        return None
    fqdn = name if name.endswith(".local") else name + ".local"
    try:
        addr = socket.gethostbyname(fqdn)
        if _is_ipv4(addr):
            return addr
    except OSError:
        pass
    if sys.platform.startswith("win"):
        return None
    ps = shutil.which("powershell.exe")
    if not ps:
        return None
    # The name only ever comes from the printer's own /printer/info, but it
    # still goes into a command line, so anything but a plain hostname is refused.
    if not all(c.isalnum() or c in "-." for c in fqdn):
        return None
    cmd = ("(Resolve-DnsName -Name '%s' -Type A -ErrorAction Stop | "
           "Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress" % fqdn)
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=RESOLVE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("powershell lookup of %s failed: %s", fqdn, exc)
        return None
    addr = (out.stdout or "").strip().splitlines()
    addr = addr[0].strip() if addr else ""
    return addr if _is_ipv4(addr) else None


def moonraker_hostname(ip: str, port: int = MOONRAKER_PORT,
                       timeout: float = SCAN_TIMEOUT) -> Optional[str]:
    try:
        r = requests.get("http://%s:%d/printer/info" % (ip, port), timeout=timeout)
        if r.status_code != 200:
            return None
        return str(((r.json() or {}).get("result") or {}).get("hostname") or "")
    except Exception:
        return None


def scan_subnet(name: str, near: str, port: int = MOONRAKER_PORT,
                probe: Callable[[str, int], Optional[str]] = None) -> Optional[str]:
    """Sweep the /24 around `near` for a Moonraker reporting hostname `name`."""
    if not name or not _is_ipv4(near):
        return None
    probe = probe or (lambda ip, p: moonraker_hostname(ip, p))
    net = ipaddress.ip_network(near + "/24", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != near]
    want = name.lower()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for ip, found in zip(hosts, pool.map(lambda ip: probe(ip, port), hosts)):
            if found and found.lower() == want:
                return ip
    return None


def find_printer(name: str, last_host: str, port: int = MOONRAKER_PORT,
                 resolver: Callable[[str], Optional[str]] = resolve_local,
                 scanner: Callable[..., Optional[str]] = scan_subnet,
                 confirm: Callable[[str, int], Optional[str]] = None) -> Optional[str]:
    """The printer's current IPv4 address, or None if it cannot be found.

    An mDNS answer is only trusted once Moonraker at that address confirms the
    hostname, so a stale cache entry cannot point the bridge somewhere else.
    """
    if not name:
        return None
    confirm = confirm or (lambda ip, p: moonraker_hostname(ip, p, timeout=3.0))
    addr = resolver(name)
    if addr:
        seen = confirm(addr, port)
        if seen and seen.lower() == name.lower():
            return addr
        log.info("%s.local resolved to %s but Moonraker there reports %r", name, addr, seen)
    if _is_ipv4(last_host):
        return scanner(name, last_host, port)
    return None
