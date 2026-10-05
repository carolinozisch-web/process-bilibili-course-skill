#!/usr/bin/env python3
"""Store ClearVault cloud API keys in Windows Credential Manager."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


CREDENTIAL_TARGET = "ClearVault/GeminiAPI"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CredentialStoreError(RuntimeError):
    pass


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


def _api():
    if os.name != "nt":
        raise CredentialStoreError("Windows 凭据管理器仅支持 Windows")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [PCREDENTIALW, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _error(action: str) -> CredentialStoreError:
    return CredentialStoreError(f"Windows 凭据管理器{action}失败（错误 {ctypes.get_last_error()}）")


def store_api_key(api_key: str) -> None:
    value = api_key.strip()
    if not value:
        raise CredentialStoreError("API Key 不能为空")
    blob_value = value.encode("utf-16-le")
    if len(blob_value) > 512:
        raise CredentialStoreError("API Key 过长，无法保存到 Windows 凭据管理器")
    blob = (wintypes.BYTE * len(blob_value)).from_buffer_copy(blob_value)
    credential = CREDENTIALW(
        Type=CRED_TYPE_GENERIC,
        TargetName=CREDENTIAL_TARGET,
        CredentialBlobSize=len(blob_value),
        CredentialBlob=ctypes.cast(blob, ctypes.POINTER(wintypes.BYTE)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        UserName="ClearVault",
    )
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise _error("保存")


def load_api_key() -> str:
    pointer = PCREDENTIALW()
    api = _api()
    if not api.CredReadW(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return ""
        raise _error("读取")
    try:
        credential = pointer.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def remove_api_key() -> None:
    if not _api().CredDeleteW(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0):
        if ctypes.get_last_error() != ERROR_NOT_FOUND:
            raise _error("删除")
