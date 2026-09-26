#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

"""
Run a local authenticated HTTP proxy forwarder for Browser3.

Chromium cannot accept upstream proxy credentials through `--proxy-server`. This
forwarder authenticates to the upstream proxy without using JavaScript or an
extension. HTTPS remains a byte-for-byte CONNECT tunnel, so TLS and HTTP/2 are
created by Chromium rather than terminated by the forwarder. For plain HTTP, the
forwarder adds `Proxy-Authorization` before relaying the request.

This module provides transport orchestration only and contains no fingerprint
masking logic.
"""
import base64
import socket
import threading
import select
import struct
import time

BUFSIZE = 65536
SOCKS5_HEADER_LIMIT = 65536
SOCKS5_HANDSHAKE_TIMEOUT = 15


def _recv_exact(sock, size, deadline=None):
    data = bytearray()
    while len(data) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("SOCKS5 handshake deadline expired")
            sock.settimeout(remaining)
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("SOCKS5 client closed the connection")
        data.extend(chunk)
    return bytes(data)


def _socks5_read_target(client, deadline=None):
    atyp = _recv_exact(client, 1, deadline)[0]
    if atyp == 0x01:
        raw = _recv_exact(client, 4, deadline)
        host = socket.inet_ntop(socket.AF_INET, raw)
        authority = host
    elif atyp == 0x03:
        length = _recv_exact(client, 1, deadline)[0]
        if not length:
            raise ValueError("empty SOCKS5 domain")
        raw = _recv_exact(client, length, deadline)
        host = raw.decode("ascii")
        if any(ord(c) <= 0x20 or ord(c) >= 0x7f for c in host):
            raise ValueError("invalid SOCKS5 domain")
        authority = host
    elif atyp == 0x04:
        raw = _recv_exact(client, 16, deadline)
        host = socket.inet_ntop(socket.AF_INET6, raw)
        authority = f"[{host}]"
    else:
        raise LookupError("unsupported SOCKS5 address type")
    port = struct.unpack(">H", _recv_exact(client, 2, deadline))[0]
    if not port:
        raise ValueError("invalid SOCKS5 port")
    return f"{authority}:{port}".encode("ascii")


def _socks5_reply(client, rep):
    # Adresa 0.0.0.0:0 je nevyužitá BND.ADDR platná pro listener pouze s CONNECT.
    client.sendall(b"\x05" + bytes([rep]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")


def _pump_tunnel(a, b):
    """Průhledný tunel bez idle limitu; předává data i jednostranné ukončení."""
    peers = {a: b, b: a}
    reading = {a, b}
    while reading:
        try:
            ready, _, _ = select.select(list(reading), [], [])
            for source in ready:
                data = source.recv(BUFSIZE)
                target = peers[source]
                if data:
                    target.sendall(data)
                else:
                    reading.discard(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
        except (OSError, ValueError):
            return


def _read_http_response_head(upstream, deadline=None):
    """Načte omezenou HTTP hlavičku a zachová případné následující bajty tunelu."""
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        if len(data) >= SOCKS5_HEADER_LIMIT:
            raise ValueError("upstream HTTP response header too large")
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("upstream HTTP response deadline expired")
            upstream.settimeout(remaining)
        chunk = upstream.recv(min(BUFSIZE, SOCKS5_HEADER_LIMIT + 1 - len(data)))
        if not chunk:
            raise ConnectionError("upstream closed before CONNECT response")
        data.extend(chunk)
    end = data.index(marker) + len(marker)
    if end > SOCKS5_HEADER_LIMIT:
        raise ValueError("upstream HTTP response header too large")
    head = bytes(data[:end])
    status_line = head.split(b"\r\n", 1)[0]
    fields = status_line.split(b" ", 2)
    if (len(fields) != 3 or fields[0] not in (b"HTTP/1.0", b"HTTP/1.1")
            or len(fields[1]) != 3 or not fields[1].isdigit()
            or any(c < 0x20 and c != 0x09 for c in fields[2])):
        raise ValueError("malformed upstream HTTP status line")
    return int(fields[1]), bytes(data[end:])


def _handle_socks5_client(client, cfg):
    upstream = None
    success_sent = False
    try:
        client_deadline = time.monotonic() + SOCKS5_HANDSHAKE_TIMEOUT
        ver, nmethods = _recv_exact(client, 2, client_deadline)
        methods = _recv_exact(client, nmethods, client_deadline)
        if ver != 0x05 or 0x00 not in methods:
            client.sendall(b"\x05\xff")
            return
        client.sendall(b"\x05\x00")

        ver, cmd, rsv = _recv_exact(client, 3, client_deadline)
        if ver != 0x05 or rsv != 0:
            _socks5_reply(client, 0x01)
            return
        if cmd != 0x01:
            _socks5_reply(client, 0x07)
            return
        try:
            authority = _socks5_read_target(client, client_deadline)
        except LookupError:
            _socks5_reply(client, 0x08)
            return
        except (ValueError, UnicodeError):
            _socks5_reply(client, 0x01)
            return

        upstream = socket.create_connection((cfg.up_host, cfg.up_port), timeout=15)
        upstream.settimeout(15)
        request = b"CONNECT " + authority + b" HTTP/1.1\r\nHost: " + authority + b"\r\n"
        request += cfg.auth_header() + b"\r\n"
        upstream.sendall(request)
        upstream_deadline = time.monotonic() + SOCKS5_HANDSHAKE_TIMEOUT
        status, surplus = _read_http_response_head(upstream, upstream_deadline)
        if status != 200:
            _socks5_reply(client, 0x05)
            return

        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        success_sent = True
        upstream.settimeout(None)
        client.settimeout(None)
        if surplus:
            client.sendall(surplus)
        _pump_tunnel(client, upstream)
    except socket.timeout:
        # Pomalý klient nesmí držet worker ani otevřený socket bez omezení.
        pass
    except (OSError, ConnectionError, ValueError):
        if not success_sent:
            try:
                _socks5_reply(client, 0x01)
            except OSError:
                pass
    finally:
        for sock in (client, upstream):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass


class Socks5ToHttpForwarder(threading.Thread):
    """Lokální SOCKS5 CONNECT adaptér pro autentizovaný HTTP proxy upstream."""

    def __init__(self, cfg: ForwarderConfig, port=0):
        super().__init__(daemon=True)
        self.cfg = cfg
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(128)
        self.port = self._srv.getsockname()[1]
        self._stopping = threading.Event()

    def run(self):
        while not self._stopping.is_set():
            try:
                client, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=_handle_socks5_client, args=(client, self.cfg),
                             daemon=True).start()

    def stop(self):
        self._stopping.set()
        try:
            self._srv.close()
        except OSError:
            pass


class ForwarderConfig:
    def __init__(self, up_host, up_port, up_user=None, up_pass=None):
        self.up_host = up_host
        self.up_port = int(up_port)
        self.up_user = up_user
        self.up_pass = up_pass

    def auth_header(self):
        if self.up_user is None:
            return b""
        token = base64.b64encode(f"{self.up_user}:{self.up_pass}".encode()).decode()
        return f"Proxy-Authorization: Basic {token}\r\n".encode()


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


def _handle_client(client, cfg: ForwarderConfig):
    upstream = None
    try:
        # Read the request line and headers through the terminating blank line.
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(BUFSIZE)
            if not chunk:
                return
            head += chunk
            if len(head) > 1 << 20:
                return
        first_line = head.split(b"\r\n", 1)[0]
        method = first_line.split(b" ", 1)[0].upper()

        upstream = socket.create_connection((cfg.up_host, cfg.up_port), timeout=30)

        if method == b"CONNECT":
            # Extract host:port from "CONNECT host:port HTTP/1.1".
            target = first_line.split(b" ")[1]
            req = b"CONNECT " + target + b" HTTP/1.1\r\n"
            req += b"Host: " + target + b"\r\n"
            req += cfg.auth_header()
            req += b"\r\n"
            upstream.sendall(req)
            # Read the upstream response and relay it to the client.
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(BUFSIZE)
                if not chunk:
                    return
                resp += chunk
            client.sendall(resp)
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                return  # The upstream rejected the request (possibly authentication).
            # From this point onward, relay the browser's TLS handshake unchanged.
            _pump(client, upstream)
        else:
            # Plain HTTP: add Proxy-Authorization and relay the complete request.
            line, rest = head.split(b"\r\n", 1)
            new_head = line + b"\r\n" + cfg.auth_header() + rest
            upstream.sendall(new_head)
            _pump(client, upstream)
    except OSError:
        pass
    finally:
        for s in (client, upstream):
            try:
                if s:
                    s.close()
            except OSError:
                pass


class ProxyForwarder(threading.Thread):
    """Loopback forwarder; port=0 asks the OS to allocate an available port."""

    def __init__(self, cfg: ForwarderConfig, port=0):
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
    # Quick test: python proxy_forwarder.py host port [user pass]
    a = sys.argv[1:]
    cfg = ForwarderConfig(*a)
    fwd = ProxyForwarder(cfg)
    fwd.start()
    print(f"Forwarder on 127.0.0.1:{fwd.port} -> {cfg.up_host}:{cfg.up_port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fwd.stop()
