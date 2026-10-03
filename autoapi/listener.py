"""Reserve listening sockets once; never steal a port or silently choose another."""
from __future__ import annotations

import errno
import socket
import sys


def server_url(host: str, port: int) -> str:
    """Bracket IPv6 literals so the displayed address is usable in a client."""
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}"


def bind_listeners(host: str, port: int, backlog: int = 2048) -> list[socket.socket]:
    """Reserve every resolved IPv4/IPv6 address, retaining sockets for Uvicorn.

    Windows must use SO_EXCLUSIVEADDRUSE, not SO_REUSEADDR: the latter can let
    two unrelated servers bind the same address. IPV6_V6ONLY matches asyncio's
    separate IPv4/IPv6 listeners. Partial failures release all reservations.
    """
    listeners: list[socket.socket] = []
    seen: set[tuple] = set()
    try:
        addresses = socket.getaddrinfo(
            host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE,
        )
        for family, sock_type, proto, _, address in addresses:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            identity = (family, address)
            if identity in seen:
                continue
            seen.add(identity)
            sock = None
            try:
                sock = socket.socket(family, sock_type, proto)
                if sys.platform == "win32":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(address)
                # SO_REUSEADDR on POSIX permits duplicate *unlistened* binds.
                # Reserve the listener as well, so collisions fail here on all
                # platforms; asyncio can safely call listen() again later.
                sock.listen(backlog)
                sock.setblocking(False)
            except OSError as exc:
                if sock is not None:
                    sock.close()
                # A resolved address may be unavailable on this machine (e.g.
                # IPv6 disabled). Never ignore in-use or permission failures.
                if exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    continue
                raise
            except BaseException:
                if sock is not None:
                    sock.close()
                raise
            listeners.append(sock)
        if not listeners:
            raise OSError(errno.EADDRNOTAVAIL, "No usable local address for the configured host")
        return listeners
    except BaseException:
        for listener in listeners:
            listener.close()
        raise


def bind_error_message(host: str, port: int, error: OSError) -> str:
    codes = {error.errno, getattr(error, "winerror", None)}
    if codes & {errno.EADDRINUSE, 10048}:
        reason = "端口已被占用，可能已启动一个 AutoAPI 实例。"
    elif codes & {errno.EACCES, errno.EPERM, 10013}:
        reason = "端口被其他程序独占，或系统禁止绑定（例如 Windows 保留端口）。"
    else:
        reason = str(error)
    return (
        f"启动失败：无法监听 {server_url(host, port)}。\n"
        f"  {reason}\n"
        "  请检查已有实例，或修改配置中的 server.host / server.port 后重试。\n"
        "  不会自动换端口，也不会停止占用端口的其他程序。"
    )
