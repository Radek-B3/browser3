#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

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

BUFSIZE = 65536


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
    """Obousměrné slepé přeposílání bajtů (tunel)."""
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
        # Přečti první řádek (a hlavičky do prázdného řádku) požadavku prohlížeče.
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
            # host:port z "CONNECT host:port HTTP/1.1"
            target = first_line.split(b" ")[1]
            req = b"CONNECT " + target + b" HTTP/1.1\r\n"
            req += b"Host: " + target + b"\r\n"
            req += cfg.auth_header()
            req += b"\r\n"
            upstream.sendall(req)
            # Přečti odpověď upstreamu (200 Connection established) a přepošli klientovi.
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(BUFSIZE)
                if not chunk:
                    return
                resp += chunk
            client.sendall(resp)
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                return  # upstream odmítl (auth?) -> ukonči
            # Od teď čistý tunel: TLS handshake prohlížeče projde beze změny.
            _pump(client, upstream)
        else:
            # Plain HTTP: vlož Proxy-Authorization do hlaviček a přepošli vše.
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
    """Lokální forwarder na 127.0.0.1:port. port=0 => OS přidělí volný."""

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
    # Rychlý test: python proxy_forwarder.py host port [user pass]
    a = sys.argv[1:]
    cfg = ForwarderConfig(*a)
    fwd = ProxyForwarder(cfg)
    fwd.start()
    print(f"Forwarder na 127.0.0.1:{fwd.port} -> {cfg.up_host}:{cfg.up_port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fwd.stop()
