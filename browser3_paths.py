#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

r"""Central paths and safe migration for Browser3 runtime data.

Masking does not live here. This module only keeps the installation tree
read-only and places mutable per-user state below ``%LOCALAPPDATA%\Browser3``.
``BROWSER3_DATA_DIR`` is an explicit development/test override.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager


INSTALL_ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve_data_root():
    override = os.environ.get("BROWSER3_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise RuntimeError(
            "LOCALAPPDATA is not set. Browser3 requires a per-user data directory; "
            "set BROWSER3_DATA_DIR explicitly for development or tests."
        )
    return os.path.join(os.path.abspath(local_app_data), "Browser3")


def reconfigure_paths():
    """Recompute all path constants from current environment variables."""
    global DATA_ROOT, PROFILES_DIR, USER_DATA_ROOT, CACHE_DIR, STATE_DIR, LOG_DIR
    global HOST_CACHE_FILE, CODEC_CACHE_FILE, GEO_CACHE_FILE, PROXY_MAP_FILE, AGENT_LOG_FILE
    global LEGACY_PROFILES_DIR, LEGACY_FILES

    DATA_ROOT = _resolve_data_root()
    PROFILES_DIR = os.path.join(DATA_ROOT, "profiles")
    USER_DATA_ROOT = os.path.join(PROFILES_DIR, "_userdata")
    CACHE_DIR = os.path.join(DATA_ROOT, "cache")
    STATE_DIR = os.path.join(DATA_ROOT, "state")
    LOG_DIR = os.path.join(DATA_ROOT, "logs")

    HOST_CACHE_FILE = os.path.join(CACHE_DIR, "host_current.json")
    CODEC_CACHE_FILE = os.path.join(CACHE_DIR, "codec_support.json")
    GEO_CACHE_FILE = os.path.join(CACHE_DIR, "proxy_geo_cache.json")
    PROXY_MAP_FILE = os.path.join(STATE_DIR, "profile_proxy_map.json")
    AGENT_LOG_FILE = os.path.join(LOG_DIR, "browser3-agent.log")

    LEGACY_PROFILES_DIR = os.path.join(INSTALL_ROOT, "profiles")
    LEGACY_FILES = {
        os.path.join(INSTALL_ROOT, "profile_proxy_map.json"): PROXY_MAP_FILE,
        os.path.join(INSTALL_ROOT, "proxy_geo_cache.json"): GEO_CACHE_FILE,
        os.path.join(INSTALL_ROOT, "gpu_profiles", "host_current.json"): HOST_CACHE_FILE,
        os.path.join(INSTALL_ROOT, "gpu_profiles", "codec_support.json"): CODEC_CACHE_FILE,
    }


reconfigure_paths()


def ensure_runtime_dirs():
    for path in (DATA_ROOT, PROFILES_DIR, USER_DATA_ROOT, CACHE_DIR, STATE_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)


@contextmanager
def file_lock(path):
    """Cross-process exclusive lock stored next to the protected state file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = path + ".lock"
    lock_file = open(lock_path, "a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            deadline = time.time() + 300
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise TimeoutError("Timed out waiting for Browser3 state lock: " + path)
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, value, *, indent=2, ensure_ascii=False):
    """Write JSON completely or leave the previous file untouched."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".browser3-", suffix=".tmp",
                                     dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=indent, ensure_ascii=ensure_ascii)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def update_json(path, update, default=None):
    """Lock, read, mutate and atomically replace one JSON state file."""
    with file_lock(path):
        current = load_json(path, default)
        result = update(current)
        write_json_atomic(path, current if result is None else result)
        return current if result is None else result


def profile_user_data_dir(profile_name):
    return os.path.join(USER_DATA_ROOT, profile_name)


def _copy_missing_tree(source, target):
    if not os.path.isdir(source):
        return
    for current, dirs, files in os.walk(source):
        relative = os.path.relpath(current, source)
        destination = target if relative == "." else os.path.join(target, relative)
        os.makedirs(destination, exist_ok=True)
        for directory in dirs:
            os.makedirs(os.path.join(destination, directory), exist_ok=True)
        for filename in files:
            src = os.path.join(current, filename)
            dst = os.path.join(destination, filename)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)


def _migrate_profiles():
    if not os.path.isdir(LEGACY_PROFILES_DIR):
        return
    _copy_missing_tree(os.path.join(LEGACY_PROFILES_DIR, "_userdata"), USER_DATA_ROOT)
    for filename in sorted(os.listdir(LEGACY_PROFILES_DIR)):
        if not (filename.startswith("profile_") and filename.endswith(".json")):
            continue
        source = os.path.join(LEGACY_PROFILES_DIR, filename)
        target = os.path.join(PROFILES_DIR, filename)
        if not os.path.exists(target):
            shutil.copy2(source, target)
        profile = load_json(target)
        if not isinstance(profile, dict):
            continue
        expected = profile_user_data_dir(os.path.splitext(filename)[0])
        if profile.get("user_data_dir") != expected:
            profile["user_data_dir"] = expected
            write_json_atomic(target, profile)


def migrate_legacy_state():
    """Copy legacy mutable data without deleting or overwriting either side."""
    ensure_runtime_dirs()
    marker = os.path.join(STATE_DIR, "migration-v1.json")
    if os.path.isfile(marker):
        return
    migration_lock = os.path.join(STATE_DIR, "legacy-migration")
    with file_lock(migration_lock):
        if os.path.isfile(marker):
            return
        _migrate_profiles()
        for source, target in LEGACY_FILES.items():
            if os.path.isfile(source) and not os.path.exists(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(source, target)
        _copy_missing_tree(os.path.join(INSTALL_ROOT, "logs"), LOG_DIR)
        write_json_atomic(marker, {"version": 1, "completed": True})


def initialize_runtime_state():
    ensure_runtime_dirs()
    migrate_legacy_state()
