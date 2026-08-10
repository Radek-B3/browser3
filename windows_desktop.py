#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

"""
Minimal Win32 lifecycle support for a headful process on an isolated desktop.
"""

import os
import secrets
import subprocess
import threading


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    DESKTOP_ALL_ACCESS = 0x000F01FF
    STARTF_USESHOWWINDOW = 0x00000001
    SW_SHOWNORMAL = 1
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    INFINITE = 0xFFFFFFFF
    STILL_ACTIVE = 259

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p,
    ]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, ENUMWINDOWSPROC,
                                          wintypes.LPARAM]
    user32.EnumDesktopWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


class DesktopProcess:
    """Malý Popen-kompatibilní obal nad procesem vytvořeným přes CreateProcessW."""

    def __init__(self, handle, pid):
        self._handle = handle
        self._lock = threading.RLock()
        self._waiters = 0
        self._close_requested = False
        self.pid = int(pid)
        self.returncode = None

    def _read_exit_code_locked(self):
        if self.returncode is not None:
            return self.returncode
        if not self._handle:
            return None
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value != STILL_ACTIVE:
            self.returncode = int(code.value)
        return self.returncode

    def _close_if_possible_locked(self):
        if self._close_requested and not self._waiters and self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def poll(self):
        with self._lock:
            result = self._read_exit_code_locked()
            self._close_if_possible_locked()
            return result

    def wait(self, timeout=None):
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            if not self._handle:
                raise RuntimeError("The process handle was closed before process termination.")
            handle = self._handle
            self._waiters += 1
        milliseconds = INFINITE if timeout is None else max(0, int(timeout * 1000))
        try:
            result = kernel32.WaitForSingleObject(handle, milliseconds)
            if result == WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired("desktop process", timeout)
            if result != WAIT_OBJECT_0:
                raise ctypes.WinError(ctypes.get_last_error())
            with self._lock:
                return self._read_exit_code_locked()
        finally:
            with self._lock:
                self._waiters -= 1
                self._close_if_possible_locked()

    def terminate(self):
        with self._lock:
            if self._read_exit_code_locked() is None:
                if not kernel32.TerminateProcess(self._handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())

    def kill(self):
        self.terminate()

    def close(self):
        with self._lock:
            self._close_requested = True
            self._close_if_possible_locked()


class IsolatedDesktop:
    """Vlastní jeden WinSta0 desktop a udržuje jej živý po dobu browser session."""

    def __init__(self, name, handle):
        self.name = name
        self.full_name = "WinSta0\\%s" % name
        self._handle = handle

    @classmethod
    def create(cls):
        if os.name != "nt":
            raise RuntimeError("An isolated desktop is supported only on Windows.")
        name = "browser3-%d-%s" % (os.getpid(), secrets.token_hex(6))
        handle = user32.CreateDesktopW(name, None, None, 0, DESKTOP_ALL_ACCESS, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return cls(name, handle)

    def launch(self, cmd, cwd=None):
        if not self._handle:
            raise RuntimeError("The desktop is already closed.")
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = self.full_name
        startup.dwFlags = STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_SHOWNORMAL
        process_info = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
        application = os.path.abspath(cmd[0])
        ok = kernel32.CreateProcessW(
            application, command_line, None, None, False,
            0, None, cwd, ctypes.byref(startup),
            ctypes.byref(process_info),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(process_info.hThread)
        return DesktopProcess(process_info.hProcess, process_info.dwProcessId)

    def windows(self):
        """Diagnostický snapshot oken na tomto desktopu; desktop nepřepíná."""
        result = []

        @ENUMWINDOWSPROC
        def collect(hwnd, _lparam):
            rect = RECT()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            result.append({
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "minimized": bool(user32.IsIconic(hwnd)),
                "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
            })
            return True

        ctypes.set_last_error(0)
        if not user32.EnumDesktopWindows(self._handle, collect, 0):
            error = ctypes.get_last_error()
            if error:
                raise ctypes.WinError(error)
        return result

    def close(self):
        if self._handle:
            if not user32.CloseDesktop(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
