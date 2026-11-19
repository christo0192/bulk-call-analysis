from __future__ import annotations

import sys
from typing import Any, cast

if sys.platform == "win32":
    import ctypes
    import threading
    from ctypes import wintypes
    from multiprocessing.connection import Connection

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    ERROR_PIPE_CONNECTED = 535
    TOKEN_USER = 1

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
    ]
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]

    def _current_user_sid() -> str:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = text.value
            if value is None:
                raise RuntimeError("Windows returned an empty user SID")
            return value
        finally:
            kernel32.LocalFree(text)

    def _security_attributes() -> tuple[SECURITY_ATTRIBUTES, wintypes.LPVOID]:
        descriptor = wintypes.LPVOID()
        size = wintypes.ULONG()
        sid = _current_user_sid()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{sid}D:(A;;GA;;;{sid})", 1, ctypes.byref(descriptor), ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
        return attrs, descriptor

    class OwnerOnlyPipeListener:
        """A multiprocessing-compatible listener with an owner-only DACL."""

        def __init__(self, address: str) -> None:
            self.address = address
            self._attrs, self._descriptor = _security_attributes()
            self._closed = False
            self._lock = threading.Lock()
            self._handles: set[int] = set()

        def accept(self) -> Connection:
            if self._closed:
                raise OSError("listener is closed")
            raw_handle = kernel32.CreateNamedPipeW(
                self.address,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                255,
                65536,
                65536,
                0,
                ctypes.byref(self._attrs),
            )
            handle: wintypes.HANDLE = (
                raw_handle
                if isinstance(raw_handle, wintypes.HANDLE)
                else wintypes.HANDLE(cast(Any, raw_handle))
            )
            if handle.value == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handle_value = handle.value
            if handle_value is None:
                raise OSError("CreateNamedPipeW returned a null handle")
            with self._lock:
                if self._closed:
                    kernel32.CloseHandle(handle)
                    raise OSError("listener is closed")
                self._handles.add(handle_value)
            if (
                not kernel32.ConnectNamedPipe(handle, None)
                and ctypes.get_last_error() != ERROR_PIPE_CONNECTED
            ):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                with self._lock:
                    self._handles.discard(handle_value)
                raise ctypes.WinError(error)
            with self._lock:
                self._handles.discard(handle_value)
            return Connection(handle_value)

        def close(self) -> None:
            with self._lock:
                self._closed = True
                handles = tuple(self._handles)
                self._handles.clear()
            for handle in handles:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
            if self._descriptor:
                kernel32.LocalFree(self._descriptor)
                self._descriptor = wintypes.LPVOID()

else:

    def _current_user_sid() -> str:
        raise OSError("Windows named pipes are unavailable on this platform")

    class OwnerOnlyPipeListener:
        def __init__(self, address: str) -> None:
            del address
            raise OSError("Windows named pipes are unavailable on this platform")
