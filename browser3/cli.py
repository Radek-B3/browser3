# SPDX-License-Identifier: MPL-2.0

"""Command-line interface for the signed Browser3 runtime cache."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .installer import (Browser3Error, Installer, UnsupportedPlatform,
                        validate_binary_override, resolve_version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browser3",
        description="Install and launch the signed Browser3 Windows x64 runtime.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="download and verify a pinned runtime")
    install.add_argument("version", nargs="?", help="explicit supported runtime pin")
    launch = commands.add_parser(
        "launch",
        help=("launch the verified runtime via launcher.py; "
              "BROWSER3_BINARY_PATH only replaces chrome.exe"),
    )
    launch.add_argument("--runtime-version", dest="runtime_version", default=None,
                        help="explicit supported runtime pin (otherwise BROWSER3_VERSION)")
    launch.add_argument("launcher_args", nargs=argparse.REMAINDER,
                        help="arguments passed unchanged to the release launcher")
    update = commands.add_parser("update", help="explicitly install another pinned runtime")
    update.add_argument("version", help="explicit target runtime pin")
    commands.add_parser("list", help="list locally installed runtime versions")
    doctor = commands.add_parser(
        "doctor", help="check platform, verified runtime root and binary state"
    )
    doctor.add_argument("version", nargs="?", help="version to diagnose")
    return parser


def _installer() -> Installer:
    return Installer()


def _cmd_install(installer: Installer, version: str | None) -> int:
    result = installer.install(version)
    print("[browser3] %s %s: %s" % ("cache hit" if result.cache_hit else "installed",
                                     result.version, "verified runtime ready"))
    return 0


def _cmd_update(installer: Installer, version: str) -> int:
    selected = resolve_version(version)
    old = sorted(installer.installed(), key=lambda row: row.get("version", ""))
    previous = old[-1]["version"] if old else "none"
    print("[browser3] explicit runtime update %s -> %s" % (previous, selected))
    print("[browser3] Chromium upgrades can change native fingerprint surfaces; existing identities are retained.")
    result = installer.install(selected)
    print("[browser3] %s %s" % ("cache hit" if result.cache_hit else "updated", result.version))
    return 0


def _cmd_list(installer: Installer) -> int:
    selected = resolve_version()
    print("selected pin: %s" % selected)
    rows = installer.installed()
    if not rows:
        print("installed runtimes: none")
        return 0
    print("installed runtimes:")
    for row in rows:
        print("  %s%s" % (row["version"], " (selected)" if row["version"] == selected else ""))
    return 0


def _cmd_doctor(installer: Installer, version: str | None) -> int:
    result = installer.doctor(version)
    for key in ("platform_supported", "data_root_writable", "selected_version", "installed",
                "runtime_root_required", "binary_override", "binary_sha256", "healthy"):
        if key in result:
            print("%s: %s" % (key, result[key]))
    return 0 if result["healthy"] else 1


def _split_launch_args(argv):
    """Consume the wrapper's option while preserving arbitrary launcher flags.

    ``argparse.REMAINDER`` only starts collecting after an explicit ``--`` when
    the remaining values look like options.  Browser3's launcher deliberately
    accepts options such as ``--profile`` directly, so parse the one wrapper
    option here and forward every other value unchanged.
    """
    runtime_version = None
    launcher_args = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--runtime-version":
            index += 1
            if index >= len(argv):
                raise Browser3Error("--runtime-version requires a value")
            runtime_version = argv[index]
        elif value.startswith("--runtime-version="):
            runtime_version = value.split("=", 1)[1]
            if not runtime_version:
                raise Browser3Error("--runtime-version requires a value")
        else:
            launcher_args.append(value)
        index += 1
    return runtime_version, launcher_args


def main(argv=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Keep launcher options (including options beginning with ``-``) opaque to
    # this package.  ``launch --help`` still goes through argparse below so the
    # command help remains available.
    if raw_argv and raw_argv[0] == "launch" and "-h" not in raw_argv and "--help" not in raw_argv:
        try:
            runtime_version, launcher_args = _split_launch_args(raw_argv[1:])
            return _installer().launch(launcher_args, runtime_version)
        except (Browser3Error, UnsupportedPlatform) as exc:
            print("browser3: %s" % exc, file=sys.stderr)
            return 2
    args = _parser().parse_args(raw_argv)
    try:
        installer = _installer()
        if args.command == "install":
            return _cmd_install(installer, args.version)
        if args.command == "launch":
            return installer.launch(args.launcher_args, args.runtime_version)
        if args.command == "update":
            return _cmd_update(installer, args.version)
        if args.command == "list":
            return _cmd_list(installer)
        if args.command == "doctor":
            return _cmd_doctor(installer, args.version)
        raise Browser3Error("Unknown browser3 command")
    except (Browser3Error, UnsupportedPlatform) as exc:
        print("browser3: %s" % exc, file=sys.stderr)
        return 2
