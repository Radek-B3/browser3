#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

"""
Run a local authenticated SOCKS5 forwarder for Browser3.

Chromium connects without authentication to a loopback SOCKS5 endpoint; this
forwarder performs optional user/password authentication with the upstream proxy.
CONNECT requests preserve remote DNS resolution. UDP ASSOCIATE is implemented for
completeness, although Chromium WebRTC did not use it in testing; non-proxied WebRTC
UDP therefore remains disabled to prevent a direct-IP leak.

This module provides transport orchestration only and contains no fingerprint
masking logic.
"""
import os
import socket
import struct
import threading
import select

BUFSIZE = 65536


class Socks5Config:
    def __init__(self, up_host, up_port, up_user=None, up_pass=None):
        self.up_host = up_host
        self.up_port = int(up_port)
        self.up_user = up_user
        self.up_pass = up_pass


# ---------- SOCKS5 helpers ----------
def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("the upstream or client closed the connection")
        buf += chunk
    return buf


def _read_addr(sock):
    """Read a SOCKS5 address and return (atyp, address bytes, host, port)."""
    atyp = _recv_exact(sock, 1)[0]
    if atyp == 0x01:      # IPv4
        addr = _recv_exact(sock, 4)
        host = socket.inet_ntoa(addr)
    elif atyp == 0x03:    # Domain name (remote DNS).
        ln = _recv_exact(sock, 1)[0]
        addr = _recv_exact(sock, ln)
        host = addr.decode("ascii", errors="replace")  # Display only; bytes remain unchanged.
    elif atyp == 0x04:    # IPv6
        addr = _recv_exact(sock, 16)
        host = socket.inet_ntop(socket.AF_INET6, addr)
    else:
        raise ValueError(f"unknown ATYP {atyp}")
    port = struct.unpack(">H", _recv_exact(sock, 2))[0]
    return atyp, addr, host, port


def _encode_addr(atyp, addr_bytes, port):
    if atyp == 0x03:  # Domain: ATYP + LEN + DOMAIN + PORT.
        return bytes([atyp, len(addr_bytes)]) + addr_bytes + struct.pack(">H", port)
    return bytes([atyp]) + addr_bytes + struct.pack(">H", port)  # IPv4/IPv6


def socks5_auth_to_upstream(cfg: Socks5Config, timeout=30):
    """Connect and authenticate to the upstream SOCKS5 server."""
    up = socket.create_connection((cfg.up_host, cfg.up_port), timeout=timeout)
    # Offer no-auth (0) and username/password (2).
    up.sendall(b"\x05\x02\x00\x02")
    ver, method = _recv_exact(up, 2)
    if method == 0x02:
        u = (cfg.up_user or "").encode()
        p = (cfg.up_pass or "").encode()
        up.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        _, status = _recv_exact(up, 2)
        if status != 0x00:
            up.close()
            raise ConnectionError("SOCKS5 upstream authentication failed")
    elif method != 0x00:
        up.close()
        raise ConnectionError(f"SOCKS5 upstream offered no acceptable authentication method (0x{method:02x})")
    return up


def socks5_open_connect(cfg: Socks5Config, dst_host, dst_port, timeout=15):
    """Open an authenticated upstream connection using remote DNS resolution."""
    up = socks5_auth_to_upstream(cfg, timeout)
    dh = dst_host.encode()
    up.sendall(b"\x05\x01\x00\x03" + bytes([len(dh)]) + dh + struct.pack(">H", int(dst_port)))
    _ver, rep, _rsv = _recv_exact(up, 3)
    _read_addr(up)  # Discard the bound address.
    if rep != 0x00:
        up.close()
        raise ConnectionError(f"SOCKS5 CONNECT rep={rep}")
    return up


def _pump(a, b):
    """Relay bytes bidirectionally without inspecting the tunnel."""
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                break
            for s in r:
                data = s.recv(BUFSIZE)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except (OSError, ValueError):
        pass


def _handle_client(client, cfg: Socks5Config):
    upstream = None
    udp_sockets = []
    try:
        # Chrome greeting; the loopback server itself requires no authentication.
        ver = _recv_exact(client, 1)[0]
        if ver != 0x05:
            return
        nmethods = _recv_exact(client, 1)[0]
        _recv_exact(client, nmethods)  # Discard the offered methods.
        client.sendall(b"\x05\x00")     # Select NO-AUTH.

        # Request from Chrome.
        ver, cmd, rsv = _recv_exact(client, 3)
        atyp, addr_bytes, host, port = _read_addr(client)
        if os.environ.get("SOCKS5_DEBUG"):
            print(f"[socks5] cmd={cmd} ({'CONNECT' if cmd==1 else 'UDP_ASSOC' if cmd==3 else cmd}) -> {host}:{port}")

        if cmd == 0x01:  # CONNECT
            upstream = socks5_auth_to_upstream(cfg)
            # Relay CONNECT with the same target (domain names use remote DNS).
            upstream.sendall(b"\x05\x01\x00" + _encode_addr(atyp, addr_bytes, port))
            up_ver, up_rep, up_rsv = _recv_exact(upstream, 3)
            up_atyp, up_addr, up_host, up_port = _read_addr(upstream)
            # Relay the reply code and upstream bound address to Chrome.
            client.sendall(b"\x05" + bytes([up_rep]) + b"\x00" +
                           _encode_addr(up_atyp, up_addr, up_port))
            if up_rep != 0x00:
                return
            _pump(client, upstream)

        elif cmd == 0x03:  # UDP ASSOCIATE
            _handle_udp_associate(client, cfg, udp_sockets)

        else:
            # Unsupported command: return 0x07 (command not supported).
            client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
    except (OSError, ConnectionError, ValueError):
        if os.environ.get("SOCKS5_DEBUG"):
            import traceback
            traceback.print_exc()
    finally:
        for s in [client, upstream] + udp_sockets:
            try:
                if s:
                    s.close()
            except OSError:
                pass


def _handle_udp_associate(client, cfg: Socks5Config, socks):
    """Relay UDP ASSOCIATE traffic between Chrome and the upstream SOCKS5 proxy.

    This lets WebRTC send STUN/UDP through the proxy, exposing the proxy exit IP
    instead of the real IP. Both sides use the same SOCKS5 datagram format, so
    datagrams are relayed unchanged.
    """
    # 1. Local UDP socket that receives Chrome datagrams.
    chrome_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    chrome_udp.bind(("127.0.0.1", 0))
    socks.append(chrome_udp)
    chrome_udp_port = chrome_udp.getsockname()[1]

    # 2. Upstream UDP ASSOCIATE; keep its TCP control connection open throughout.
    up_ctrl = socks5_auth_to_upstream(cfg)
    socks.append(up_ctrl)
    up_ctrl.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00" + struct.pack(">H", 0))
    _v, rep, _r = _recv_exact(up_ctrl, 3)
    up_atyp, up_addr, up_host, up_port = _read_addr(up_ctrl)
    if rep != 0x00:
        client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")  # general failure
        return
    # If the upstream reports 0.0.0.0, use the proxy host as its relay address.
    relay_host = up_host if up_host not in ("0.0.0.0", "") else cfg.up_host
    up_relay = (socket.gethostbyname(relay_host), up_port)

    # 3. UDP socket for the upstream relay.
    up_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    socks.append(up_udp)

    # 4. Tell Chrome to send datagrams to our local UDP socket.
    client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") +
                   struct.pack(">H", chrome_udp_port))

    # 5. Relay until Chrome or the upstream closes its TCP control connection.
    chrome_src = None
    while True:
        r, _, _ = select.select([chrome_udp, up_udp, client, up_ctrl], [], [], 120)
        if not r:
            break
        if client in r and not client.recv(BUFSIZE):
            break            # Chrome closed the control connection.
        if up_ctrl in r and not up_ctrl.recv(BUFSIZE):
            break            # The upstream closed the control connection.
        if chrome_udp in r:
            data, chrome_src = chrome_udp.recvfrom(BUFSIZE)
            up_udp.sendto(data, up_relay)          # Relay unchanged to the upstream.
        if up_udp in r:
            data, _ = up_udp.recvfrom(BUFSIZE)
            if chrome_src:
                chrome_udp.sendto(data, chrome_src)  # Relay unchanged to Chrome.


class Socks5Forwarder(threading.Thread):
    """Loopback SOCKS5 forwarder; port=0 asks the OS for an available port."""

    def __init__(self, cfg: Socks5Config, port=0):
        super().__init__(daemon=True)
        self.cfg = cfg
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(128)
        self.port = self._srv.getsockname()[1]
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                client, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(
                target=_handle_client, args=(client, self.cfg), daemon=True
            ).start()

    def stop(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    cfg = Socks5Config(*a)
    fwd = Socks5Forwarder(cfg)
    fwd.start()
    print(f"SOCKS5 forwarder on 127.0.0.1:{fwd.port} -> {cfg.up_host}:{cfg.up_port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fwd.stop()
