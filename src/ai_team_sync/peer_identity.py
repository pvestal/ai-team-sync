"""Which local OS account opened the connection an ATS request arrived on.

The ATS API is unauthenticated, so a worker label is a claim, not an identity.
For a worker class that declares an OS binding, the server asks the kernel
instead of the label: /proc/net/tcp and /proc/net/tcp6 list every TCP socket
with the uid that created it, and the CLIENT end of an accepted loopback
connection is exactly one of those rows.

Three rules make that answer trustworthy rather than merely available:

1. Exact 4-tuple, direction-sensitive. The row must have local == the request's
   client address AND remote == the request's server address, in ESTABLISHED
   state. The server's own accepted socket is the same pair reversed and is
   owned by ATS, so matching on the client port alone can read the wrong row.
2. More than one distinct owner, or none, is unknown. Unknown is never a match.
3. A request carrying ANY forwarding header is unidentifiable. uvicorn trusts
   X-Forwarded-For from 127.0.0.1 by default and rewrites scope["client"] to the
   host AND PORT it names; demonstrated 2026-09-13 (#2741): a uid-1000 caller
   naming a uid-993 process's live source port was read as uid 993. uvicorn only
   rewrites when the header is present and never removes it, so refusing on the
   header closes that regardless of how the server was launched. server.main
   also disables proxy headers, but this check must not depend on it.

What this is and is not: it distinguishes OS accounts. It does not stop root,
anything root-equivalent (on Tower, membership of the docker group is), or other
code running as the bound account itself.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Iterable

PROC_NET = Path("/proc/net")

_ESTABLISHED = "01"
_FORWARDING_HEADERS = frozenset({
    b"x-forwarded-for", b"forwarded", b"x-real-ip",
    b"x-forwarded-host", b"x-forwarded-port", b"x-client-ip",
})

Endpoint = tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]


def _unmap(addr):
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _decode(field: str, v6: bool) -> Endpoint:
    addr_hex, port_hex = field.split(":")
    raw = bytes.fromhex(addr_hex)
    if v6:
        # Each of the four 32-bit words is printed in host byte order.
        addr = ipaddress.IPv6Address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    else:
        addr = ipaddress.IPv4Address(raw[::-1])
    return _unmap(addr), int(port_hex, 16)


def socket_owners(table: str, v6: bool, local: Endpoint, remote: Endpoint) -> set[int]:
    """uids of ESTABLISHED sockets in one /proc/net table whose local end is
    `local` and remote end is `remote`."""
    found: set[int] = set()
    for row in table.splitlines()[1:]:
        fields = row.split()
        if len(fields) < 8 or fields[3] != _ESTABLISHED:
            continue
        try:
            if _decode(fields[1], v6) != local or _decode(fields[2], v6) != remote:
                continue
            found.add(int(fields[7]))
        except (ValueError, IndexError):
            continue
    return found


def _endpoint(value: Any) -> Endpoint | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    host, port = value
    if not isinstance(host, str) or type(port) is not int or not 0 < port < 65536:
        return None
    try:
        return _unmap(ipaddress.ip_address(host)), port
    except ValueError:
        return None


def has_forwarding_header(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    return any(bytes(name).lower() in _FORWARDING_HEADERS for name, _ in headers or ())


def peer_uid_from_scope(scope: dict, *, proc_net: Path = PROC_NET) -> int | None:
    """The uid that owns the client end of this request's connection, or None."""
    if has_forwarding_header(scope.get("headers") or ()):
        return None
    client = _endpoint(scope.get("client"))
    server = _endpoint(scope.get("server"))
    if client is None or server is None:
        return None
    if not (client[0].is_loopback and server[0].is_loopback):
        return None
    owners: set[int] = set()
    ours: set[int] = set()
    for name, v6 in (("tcp", False), ("tcp6", True)):
        try:
            table = (proc_net / name).read_text()
        except OSError:
            continue
        owners |= socket_owners(table, v6, client, server)
        ours |= socket_owners(table, v6, server, client)
    # The accepted socket this request arrived on must itself still be
    # ESTABLISHED and owned by this server. If the caller already closed, ours is
    # CLOSE_WAIT and the 4-tuple could in principle be re-established by some
    # other process before this lookup runs; refuse rather than attribute that.
    if ours != {os.geteuid()}:
        return None
    return owners.pop() if len(owners) == 1 else None


def peer_uid_for_request(request) -> int | None:
    scope = getattr(request, "scope", None)
    return peer_uid_from_scope(scope) if isinstance(scope, dict) else None
