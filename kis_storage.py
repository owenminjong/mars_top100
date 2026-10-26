"""Opt-in password + current-user DPAPI vault; session-only by default."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import hashlib
import math
import os
from pathlib import Path
import tempfile

from mars_core import DataError, user_data_dir


MAGIC = b"MARS-KIS-VAULT-2\x00"
SALT_SIZE = 32
MAX_VAULT_SIZE = 65536


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise DataError("잠금 비밀번호는 12~128자로 설정하세요. 길고 고유한 문장을 권장합니다")


def password_key(password, salt):
    validate_password(password)
    # Version 2 fixes the KDF parameters: untrusted files cannot lower the cost.
    return bytearray(hashlib.scrypt(password.encode("utf-8"), salt=salt,
        n=2**17, r=8, p=1, dklen=32, maxmem=256 * 1024 * 1024))


def wipe(value):
    if value is not None:
        value[:] = b"\x00" * len(value)


def dpapi(data: bytes, decrypt=False, entropy=None) -> bytes:
    if os.name != "nt":
        raise DataError("인증정보 저장은 Windows 10/11에서 지원합니다")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    extra_buf = ctypes.create_string_buffer(bytes(entropy)) if entropy is not None else None
    extra = Blob(len(entropy), ctypes.cast(extra_buf, ctypes.POINTER(ctypes.c_ubyte))) if extra_buf else None
    result = Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately NOT CRYPTPROTECT_LOCAL_MACHINE.
    try:
        if not function(ctypes.byref(source), None, ctypes.byref(extra) if extra is not None else None,
                        None, None, 1, ctypes.byref(result)):
            raise DataError("키 저장소를 열 수 없습니다. 비밀번호·Windows 계정 또는 파일 손상을 확인하세요")
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buf, 0, len(buf))
        if extra_buf is not None:
            ctypes.memset(extra_buf, 0, len(extra_buf))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as f:
            name = f.name
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class SecureStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else user_data_dir()
        self.path = self.directory / "kis_credentials.dpapi"
        self.cooldown_path = self.directory / "kis_token_cooldown.json"
        self.entropy = None
        self.salt = None

    @property
    def persistent(self):
        return self.entropy is not None

    def saved_kind(self):
        try:
            with self.path.open("rb") as f:
                return "password" if f.read(len(MAGIC)) == MAGIC else "legacy"
        except FileNotFoundError:
            return "none"
        except OSError:
            raise DataError("저장된 키 파일에 접근할 수 없습니다") from None

    def load(self):
        """Startup must NEVER decrypt credentials, including legacy DPAPI files."""
        return {"last_token_attempt": self.last_attempt()}

    def last_attempt(self):
        try:
            with self.cooldown_path.open("rb") as f:
                value = json.loads(f.read(256)).get("last_token_attempt", 0)
            return value if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0 else 0
        except (OSError, ValueError, AttributeError):
            return 0

    def record_attempt(self, value):
        if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            return
        try:
            atomic_write(self.cooldown_path, json.dumps({"last_token_attempt": value}).encode("ascii"))
        except OSError:
            raise DataError("인증 요청 대기시간을 저장하지 못했습니다") from None

    def _read(self):
        try:
            with self.path.open("rb") as f:
                raw = f.read(MAX_VAULT_SIZE + 1)
            if len(raw) > MAX_VAULT_SIZE:
                raise ValueError()
            return raw
        except (OSError, ValueError):
            raise DataError("저장된 키 파일을 읽을 수 없습니다") from None

    @staticmethod
    def _payload(raw, version):
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("version") != version:
                raise ValueError()
            for name in ("app_key", "app_secret"):
                value = payload.get(name)
                if not isinstance(value, str) or not 10 <= len(value) <= 512 or any(not 33 <= ord(c) <= 126 for c in value):
                    raise ValueError()
            token = payload.get("access_token")
            if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 16384
                    or any(not 33 <= ord(c) <= 126 for c in token)):
                raise ValueError()
            for name in ("expires_at", "last_token_attempt"):
                value = payload.get(name, 0)
                if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                    raise ValueError()
            return payload
        except (ValueError, TypeError, UnicodeError):
            raise DataError("저장된 키의 형식이 올바르지 않습니다. 키를 다시 설정하세요") from None

    def unlock(self, password):
        raw = self._read()
        if not raw.startswith(MAGIC) or len(raw) <= len(MAGIC) + SALT_SIZE:
            raise DataError("비밀번호 저장소가 아닙니다. 기존 키 전환 또는 새 키 설정을 이용하세요")
        salt = raw[len(MAGIC):len(MAGIC) + SALT_SIZE]
        entropy = password_key(password, salt)
        try:
            payload = self._payload(dpapi(raw[len(MAGIC) + SALT_SIZE:], decrypt=True, entropy=entropy), 2)
        except Exception:
            wipe(entropy)
            raise
        self.lock()
        self.salt, self.entropy = salt, entropy
        return payload

    def read_legacy(self):
        raw = self._read()
        if raw.startswith(MAGIC):
            raise DataError("이미 비밀번호로 보호된 키입니다. 잠금 해제를 이용하세요")
        return self._payload(dpapi(raw, decrypt=True), 1)

    def install(self, payload, password=None):
        """Commit only after validation; failures leave the old vault/mode intact."""
        if password is None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                raise DataError("기존 저장 키를 삭제하지 못해 이번 실행 전용 모드로 전환하지 않았습니다") from None
            self.lock()
            return
        salt = os.urandom(SALT_SIZE)
        entropy = password_key(password, salt)
        try:
            self._write(payload, salt, entropy)
        except Exception:
            wipe(entropy)
            raise
        self.lock()
        self.salt, self.entropy = salt, entropy

    def _write(self, payload, salt, entropy):
        try:
            # Explicit fields only; a caller cannot accidentally persist a password.
            allowed = ("app_key", "app_secret", "access_token", "expires_at", "last_token_attempt")
            data = {name: payload[name] for name in allowed if name in payload}
            data["version"] = 2
            encrypted = dpapi(json.dumps(data).encode("utf-8"), entropy=entropy)
            atomic_write(self.path, MAGIC + salt + encrypted)
        except OSError:
            raise DataError("KIS 설정 저장 실패: 사용자 데이터 폴더의 쓰기 권한과 디스크 공간을 확인하세요") from None

    def save(self, payload):
        self.record_attempt(payload.get("last_token_attempt", 0))
        if self.persistent:
            self._write(payload, self.salt, self.entropy)

    def lock(self):
        wipe(self.entropy)
        self.entropy = self.salt = None

    def delete(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise DataError("저장된 KIS 설정을 삭제하지 못했습니다") from None
        finally:
            self.lock()


def purge_legacy_data(directory=None):
    """Remove only the former app's known Naver databases/logs, never migrate them."""
    directory = Path(directory) if directory is not None else user_data_dir()
    for base in ("history.sqlite3", "krx_daily.sqlite3"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            (directory / (base + suffix)).unlink(missing_ok=True)
    for name in ("mars.log", "mars.log.1", "mars.log.2"):
        (directory / name).unlink(missing_ok=True)


class InstanceLock:
    """One app per Windows user: avoids token issuance races across EXE processes."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else user_data_dir()
        self.file = None

    def acquire(self):
        import msvcrt
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = (self.directory / "kis_app.lock").open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            self.file = None
            raise DataError("마스 KIS가 이미 실행 중입니다. 기존 창을 이용하세요") from None

    def close(self):
        if self.file:
            self.file.close()
            self.file = None
