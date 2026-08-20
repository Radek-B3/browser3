#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0
"""Local Browser3 Session API v1 (Python 3.7 compatible)."""

import argparse
import base64
import hashlib
import http.server
import json
import logging
import logging.handlers
import os
import secrets
import signal
import socket
import struct
import threading
import time
import urllib.parse
from datetime import datetime

import browser3_paths as paths
import launcher


ROOT = os.path.dirname(os.path.abspath(__file__))
API_VERSION = "v1"
MAX_REQUEST_BODY = 64 * 1024
ALLOWED_BUILDS = ("Release", "Release2", "Dev")
ALLOWED_CONTROLS = launcher.CONTROL_MODES
ALLOWED_DESKTOPS = launcher.DESKTOP_MODES
ACTIVE_STATES = ("starting", "ready", "stopping")


def launch_exited_early(error):
    """Classify early-exit errors without depending on a localized message."""
    text = str(error).lower()
    return "exit" in text or "exited" in text


class ApiError(RuntimeError):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self):
        return {"error": {"code": self.code, "message": self.message,
                          "details": self.details}}


def utc_now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def send_browser_close(cdp_url, timeout=3.0):
    """Send one masked `Browser.close` WebSocket frame without a dependency."""
    parsed = urllib.parse.urlsplit(cdp_url)
    if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("CDP URL must be a loopback ws:// endpoint.")
    port = parsed.port
    if not port:
        raise ValueError("CDP URL does not contain a port.")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (path, parsed.hostname, port, key)
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 16384:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError("CDP WebSocket handshake failed: %r" % status_line)
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest())
        if b"sec-websocket-accept: " + expected.lower() not in response.lower():
            raise RuntimeError("CDP WebSocket returned an invalid accept key.")

        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        if len(payload) < 126:
            header = bytes((0x81, 0x80 | len(payload)))
        else:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(payload))
        sock.sendall(header + mask + masked)
    finally:
        sock.close()


class SessionManager:
    def __init__(self, launch_func=None, cdp_timeout=30.0, close_timeout=10.0,
                 logger=None):
        self.launch_func = launch_func or launcher.launch_one
        self.cdp_timeout = cdp_timeout
        self.close_timeout = close_timeout
        self.logger = logger or logging.getLogger("browser3-agent")
        self._sessions = {}
        self._profile_owners = {}
        self._lock = threading.RLock()

    @staticmethod
    def _public(session):
        return {key: value for key, value in session.items()
                if not key.startswith("_")}

    def _set_status(self, session, status, error=None):
        session["status"] = status
        session["updated_at"] = utc_now()
        session["error"] = error

    def _validate_create(self, data):
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_request", "The JSON body must be an object.")
        profile = data.get("profile")
        if isinstance(profile, bool) or not isinstance(profile, int) or profile < 1:
            raise ApiError(400, "invalid_request", "`profile` must be a positive integer.")
        proxy = data.get("proxy", False)
        if not isinstance(proxy, bool):
            raise ApiError(400, "invalid_request", "`proxy` must be a boolean.")
        build = data.get("build", "Release")
        if build not in ALLOWED_BUILDS:
            raise ApiError(400, "unsupported_build",
                           "Supported builds: %s." % ", ".join(ALLOWED_BUILDS),
                           {"build": build, "allowed_builds": list(ALLOWED_BUILDS)})
        control = data.get("control", "cdp")
        if control not in ALLOWED_CONTROLS:
            raise ApiError(400, "unsupported_control", "Unsupported control mode.",
                           {"control": control})
        desktop = data.get("desktop", "current")
        if desktop not in ALLOWED_DESKTOPS:
            raise ApiError(400, "unsupported_desktop", "Unsupported desktop mode.",
                           {"desktop": desktop})
        try:
            launcher.load_profile(profile)
        except SystemExit:
            raise ApiError(404, "profile_not_found", "The profile does not exist.",
                           {"profile": profile})
        return profile, proxy, build, control, desktop

    def create(self, data):
        profile, proxy, build, control, desktop = self._validate_create(data)
        session_id = "ses_" + secrets.token_hex(16)
        now = utc_now()
        session = {
            "session_id": session_id, "status": "starting", "profile": profile,
            "proxy": proxy, "build": build, "control": control,
            "desktop": desktop, "desktop_name": None, "cdp_url": None,
            "created_at": now, "updated_at": now, "error": None, "_launch": None,
        }
        with self._lock:
            owner = self._profile_owners.get(profile)
            if owner:
                raise ApiError(409, "profile_in_use",
                               "Profile %d is already used by another session." % profile,
                               {"profile": profile, "session_id": owner})
            self._profile_owners[profile] = session_id
            self._sessions[session_id] = session

        try:
            chrome_exe = launcher.chrome_exe_path(build)
            if not os.path.exists(chrome_exe):
                raise ApiError(500, "browser_not_found", "%s chrome.exe does not exist." % build,
                               {"build": build, "path": chrome_exe})
            launch = self.launch_func(profile, proxy, False, chrome_exe,
                                      control=control, cdp_timeout=self.cdp_timeout,
                                      desktop=desktop)
            if launch is None:
                raise ApiError(500, "browser_start_failed", "Launcher returned no process.")
            with self._lock:
                session["_launch"] = launch
                session["cdp_url"] = launch.cdp_url
                session["desktop_name"] = (
                    launch.desktop.full_name if getattr(launch, "desktop", None) else None
                )
                self._set_status(session, "ready")
            watcher = threading.Thread(target=self._watch_process,
                                       args=(session_id, launch), daemon=True)
            watcher.start()
            self.logger.info("session_ready id=%s profile=%d control=%s desktop=%s pid=%d",
                             session_id, profile, control, desktop, launch.proc.pid)
            return self._public(session)
        except ApiError as exc:
            self._fail_start(session, exc.code, exc.message)
            raise
        except launcher.ProfileInUseError as exc:
            self._fail_start(session, "profile_in_use", str(exc))
            raise ApiError(409, "profile_in_use", str(exc), {"profile": profile})
        except launcher.BrowserStartError as exc:
            code = "browser_exited_early" if launch_exited_early(exc) else "cdp_timeout"
            self._fail_start(session, code, str(exc))
            raise ApiError(502 if code == "browser_exited_early" else 504, code, str(exc))
        except Exception as exc:
            self._fail_start(session, "browser_start_failed", str(exc))
            raise ApiError(500, "browser_start_failed", "The browser could not be started.")

    def _fail_start(self, session, code, message):
        with self._lock:
            self._set_status(session, "failed", {"code": code, "message": message})
            self._profile_owners.pop(session["profile"], None)
        self.logger.error("session_failed id=%s profile=%d code=%s",
                          session["session_id"], session["profile"], code)

    def _watch_process(self, session_id, launch):
        launch.proc.wait()
        launch.cleanup()
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            if session["status"] == "ready":
                if launch.proc.returncode not in (None, 0):
                    self._set_status(session, "failed", {
                        "code": "browser_exited_early",
                        "message": "Chrome exited unexpectedly (exit %s)." % launch.proc.returncode,
                    })
                else:
                    self._set_status(session, "stopped")
            self._profile_owners.pop(session["profile"], None)

    def list(self):
        with self._lock:
            return [self._public(session) for session in self._sessions.values()]

    def get(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ApiError(404, "session_not_found", "The session does not exist.",
                               {"session_id": session_id})
            return self._public(session)

    def delete(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ApiError(404, "session_not_found", "The session does not exist.",
                               {"session_id": session_id})
            if session["status"] in ("stopped", "failed"):
                return self._public(session)
            self._set_status(session, "stopping")
            launch = session["_launch"]

        if launch:
            if launch.cdp_url and launch.proc.poll() is None:
                try:
                    send_browser_close(launch.cdp_url)
                except Exception as exc:
                    self.logger.warning("browser_close_failed id=%s error=%s",
                                        session_id, exc.__class__.__name__)
            if launch.proc.poll() is None:
                try:
                    launch.proc.wait(timeout=self.close_timeout)
                except Exception:
                    launch.terminate(timeout=2.0)
            launch.cleanup()

        with self._lock:
            self._set_status(session, "stopped")
            self._profile_owners.pop(session["profile"], None)
            result = self._public(session)
        self.logger.info("session_stopped id=%s profile=%d", session_id,
                         session["profile"])
        return result

    def shutdown(self):
        with self._lock:
            ids = [sid for sid, session in self._sessions.items()
                   if session["status"] in ACTIVE_STATES]
        for session_id in ids:
            try:
                self.delete(session_id)
            except Exception:
                self.logger.exception("session_cleanup_failed id=%s", session_id)

    def active_count(self):
        with self._lock:
            return sum(1 for session in self._sessions.values()
                       if session["status"] in ACTIVE_STATES)


class AgentHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager):
        self.manager = manager
        super().__init__(address, AgentRequestHandler)


class AgentRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "browser3-agent/1"

    def log_message(self, fmt, *args):
        self.server.manager.logger.info("http client=%s " + fmt,
                                        self.client_address[0], *args)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error):
        self._json(error.status, error.payload())

    def _read_json(self):
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_json", "A valid Content-Length header is required.")
        if length < 0 or length > MAX_REQUEST_BODY:
            raise ApiError(400, "invalid_request", "The request body is too large.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "invalid_json", "The body is not valid UTF-8 JSON.")

    def do_GET(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/health":
                self._json(200, {"status": "ok", "api_version": API_VERSION,
                                 "active_sessions": self.server.manager.active_count()})
            elif path == "/v1/sessions":
                self._json(200, {"sessions": self.server.manager.list()})
            elif path.startswith("/v1/sessions/"):
                self._json(200, self.server.manager.get(path.rsplit("/", 1)[1]))
            else:
                raise ApiError(404, "not_found", "The endpoint does not exist.")
        except ApiError as exc:
            self._error(exc)
        except Exception:
            self.server.manager.logger.exception("unexpected GET error")
            self._error(ApiError(500, "internal_error", "Internal agent error."))

    def do_POST(self):
        try:
            if urllib.parse.urlsplit(self.path).path != "/v1/sessions":
                raise ApiError(404, "not_found", "The endpoint does not exist.")
            self._json(201, self.server.manager.create(self._read_json()))
        except ApiError as exc:
            self._error(exc)
        except Exception:
            self.server.manager.logger.exception("unexpected POST error")
            self._error(ApiError(500, "internal_error", "Internal agent error."))

    def do_DELETE(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if not path.startswith("/v1/sessions/"):
                raise ApiError(404, "not_found", "The endpoint does not exist.")
            self._json(200, self.server.manager.delete(path.rsplit("/", 1)[1]))
        except ApiError as exc:
            self._error(exc)
        except Exception:
            self.server.manager.logger.exception("unexpected DELETE error")
            self._error(ApiError(500, "internal_error", "Internal agent error."))


def configure_logging(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logger = logging.getLogger("browser3-agent")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def validate_listen(value):
    if value not in ("127.0.0.1", "localhost"):
        raise argparse.ArgumentTypeError("The agent may listen only on loopback.")
    return value


def main():
    paths.initialize_runtime_state()
    parser = argparse.ArgumentParser(description="Local Browser3 Session API v1")
    parser.add_argument("--listen", type=validate_listen, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17890)
    parser.add_argument("--cdp-timeout", type=float, default=30.0)
    parser.add_argument("--close-timeout", type=float, default=10.0)
    parser.add_argument("--log", default=paths.AGENT_LOG_FILE)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be in the range 0..65535")

    logger = configure_logging(args.log)
    manager = SessionManager(cdp_timeout=args.cdp_timeout,
                             close_timeout=args.close_timeout, logger=logger)
    server = AgentHTTPServer((args.listen, args.port), manager)
    stopping = threading.Event()

    def stop_server(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_server)
    host, port = server.server_address
    print(json.dumps({"status": "ready", "api_version": API_VERSION,
                      "listen": host, "port": port}), flush=True)
    logger.info("agent_started listen=%s port=%d", host, port)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        manager.shutdown()
        server.server_close()
        logger.info("agent_stopped")


if __name__ == "__main__":
    main()
