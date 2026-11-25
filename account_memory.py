"""Ordered school logins encrypted by Windows for the current Windows user."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import time
from urllib.parse import urlsplit

from live_platform import archive_origins


_MAX_BYTES = 1024 * 1024
_INVALID = "无法读取当前学校保存的账号。请手动输入，或清除本机记忆后重新保存。"
_UNAVAILABLE = "此设备无法使用 Windows 用户加密，请手动输入账号和密码。"
_FILE_ACCESS = Lock()


def _school_key(base):
    invalid = "请填写有效的学校平台地址。"
    if not isinstance(base, str) or re.search(r"[\s\x00-\x1f\x7f?#\\]", base):
        raise ValueError(invalid)
    try:
        url = urlsplit(base)
        host, port = url.hostname, url.port
    except ValueError:
        raise ValueError(invalid) from None
    if (url.scheme not in ("http", "https") or not host or "@" in url.netloc or port == 0):
        raise ValueError(invalid)
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if url.scheme == "https" else 80):
        authority += f":{port}"
    normalized = f"{url.scheme}://{authority}{url.path.rstrip('/')}/"
    school = archive_origins(normalized)[0]
    return sha256(school.encode("utf-8")).digest()


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    @classmethod
    def from_bytes(cls, data):
        # ctypes keeps the backing array alive through the structure's pointer field.
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return cls(len(data), buffer)


@lru_cache(maxsize=1)
def _windows_crypto():
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    blob = ctypes.POINTER(_Blob)
    common = [blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob]
    crypt.CryptProtectData.argtypes = [blob, wintypes.LPCWSTR, *common]
    crypt.CryptUnprotectData.argtypes = [blob, ctypes.POINTER(wintypes.LPWSTR), *common]
    crypt.CryptProtectData.restype = crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return crypt, kernel


def _crypt(data, scope, *, encrypt):
    crypt, kernel = _windows_crypto()
    source = _Blob.from_bytes(data)
    entropy = _Blob.from_bytes(b"SIMtrade/account-memory/v1/" + scope)
    output = _Blob()
    operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    try:
        # CRYPTPROTECT_UI_FORBIDDEN (1); current-user encryption is the default scope.
        if not operation(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
            raise ValueError("Windows 未能加密本次账号信息。" if encrypt else _INVALID)
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(source.data, 0, source.size)
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)


def _file_access(operation):
    # Windows can deny opening or replacing a file while another process replaces
    # it. Serialize our own file handles and retry transient OS sharing conflicts.
    with _FILE_ACCESS:
        for attempt in range(16):
            try:
                return operation()
            except PermissionError as exc:
                # CRT open() can expose EACCES alone, without a winerror.
                if getattr(exc, "winerror", None) not in (None, 5, 32, 33) or attempt == 15:
                    raise
                time.sleep(min(0.015 * (attempt + 1), 0.1))


def _valid(accounts):
    return (isinstance(accounts, dict) and 1 <= len(accounts) <= 10 and
            all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", k) and
                isinstance(v, str) for k, v in accounts.items()))


class AccountMemory:
    def __init__(self, data):
        self.directory = Path(data) / "账号记忆"
        self.available = False
        if os.name == "nt":
            try:
                _windows_crypto()
                self.available = True
            except (AttributeError, OSError):
                pass

    def _read(self, base):
        if not self.available:
            return {}
        scope = _school_key(base)
        path = self.directory / (scope.hex() + ".bin")
        def read_blob():
            with path.open("rb") as stream:
                return stream.read(_MAX_BYTES + 1)
        try:
            blob = _file_access(read_blob)
            if not 0 < len(blob) <= _MAX_BYTES:
                raise ValueError(_INVALID)
            rows = json.loads(_crypt(blob, scope, encrypt=False))
            if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
                raise ValueError(_INVALID)
            if any(not isinstance(row, list) or len(row) != 2 for row in rows):
                raise ValueError(_INVALID)
            accounts = dict(rows)
            if len(accounts) != len(rows) or not _valid(accounts):
                raise ValueError(_INVALID)
            return accounts
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, RecursionError):
            raise ValueError(_INVALID) from None

    def accounts(self, base):
        return list(self._read(base))

    def credentials(self, base, requested_ids):
        saved = self._read(base)
        return {account: saved[account] for account in requested_ids if account in saved}

    def save(self, base, accounts):
        if not self.available:
            raise ValueError(_UNAVAILABLE)
        if not _valid(accounts):
            raise ValueError("记忆的账号组须包含 1–10 个有效账号。")
        scope = _school_key(base)
        payload = json.dumps(list(accounts.items()), separators=(",", ":")).encode("ascii")
        if len(payload) >= _MAX_BYTES:
            raise ValueError("本次账号信息过长，无法保存。")
        sealed = _crypt(payload, scope, encrypt=True)
        if len(sealed) > _MAX_BYTES:
            raise ValueError("本次账号信息过长，无法保存。")
        destination = self.directory / (scope.hex() + ".bin")
        tmp = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                tmp = Path(stream.name)
                stream.write(sealed)
                stream.flush()
                os.fsync(stream.fileno())
            _file_access(lambda: os.replace(tmp, destination))
        except OSError:
            raise ValueError("无法写入本机账号记忆，请检查工作台数据目录是否可写。") from None
        finally:
            if tmp is not None and tmp.exists():
                try:
                    _file_access(lambda: tmp.unlink(missing_ok=True))
                except OSError:
                    pass

    def forget(self, base):
        scope = _school_key(base)
        path = self.directory / (scope.hex() + ".bin")
        try:
            _file_access(lambda: path.unlink(missing_ok=True))
        except OSError:
            raise ValueError("未能清除本机账号记忆，请检查工作台数据目录权限。") from None
