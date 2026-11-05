"""Opt-in device identity for the effort export (#140).

``mool device init`` writes ``~/.moolmesh/device.json`` (mode 600):

- ``secret``: 32 random bytes (hex). It never leaves the machine: what an
  export carries is ``HMAC-SHA256(secret, org)``, so the same device reporting
  to two organizations shows two unrelated ids.
- ``label``: optional free text chosen by the person (e.g. "laptop").
- ``fingerprint``: SHA-256 of a machine identifier (Linux ``machine-id``,
  macOS ``IOPlatformUUID``, Windows ``MachineGuid``; the hostname only as a
  last resort). Only the hash is stored. A copied ``device.json`` (dotfile
  sync, a restored backup on another machine) no longer matches, and the
  export refuses it until ``mool device init --reset``.

Nothing is created unless the person runs ``mool device init``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
_FP_DOMAIN = b"moolmesh-device-fingerprint-v1\0"

# Exit codes per DeviceError reason (CLI).
EXIT_CODES = {"exists": 1, "missing": 3, "invalid": 3, "copied": 3, "bad_args": 2}


class DeviceError(Exception):
    """A device file that cannot be used; ``reason`` is a stable token."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.reason, 1)


def device_path() -> Path:
    """``~/.moolmesh/device.json`` (resolved at call time: tests move HOME)."""
    return Path.home() / ".moolmesh" / "device.json"


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _machine_identifier() -> tuple[str, str]:
    """``(source, raw identifier)`` of this machine; raw is never stored."""
    if sys.platform.startswith("linux"):
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            raw = _read_text(path)
            if raw:
                return "machine-id", raw
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out or "")
        if m:
            return "ioplatformuuid", m.group(1)
    elif sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
            if value:
                return "machineguid", str(value)
        except OSError:
            pass
    return "hostname", socket.gethostname() or "unknown"


def machine_fingerprint() -> tuple[str, str]:
    """``(source, sha256 hex)`` of this machine's identifier."""
    source, raw = _machine_identifier()
    digest = hashlib.sha256(_FP_DOMAIN + raw.strip().lower().encode("utf-8"))
    return source, digest.hexdigest()


def org_key(org: str) -> str:
    """Normalized organization name: GitHub logins are case-insensitive."""
    return str(org or "").strip().lower()


def device_id(secret_hex: str, org: str) -> str:
    """The id an export carries for ``org``: ``HMAC-SHA256(secret, org)``."""
    return hmac.new(bytes.fromhex(secret_hex), org_key(org).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _write(path: Path, data: dict[str, Any]) -> None:
    from hub.snapshot import write_atomic
    write_atomic(path, json.dumps(data, sort_keys=True, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - chmod is best-effort on Windows
        pass


def init_device(label: str | None = None, *, reset: bool = False,
                path: Path | None = None) -> dict[str, Any]:
    """Create ``device.json``; with ``reset``, replace it (new secret = new ids)."""
    path = path or device_path()
    if path.exists() and not reset:
        raise DeviceError(
            "exists", f"{path} already exists; use mool device init --reset to "
            "replace it (this changes the exported ids)")
    source, fp = machine_fingerprint()
    data = {
        "format": FORMAT_VERSION,
        "secret": secrets.token_bytes(32).hex(),
        "label": (label or "").strip() or None,
        "fingerprint": fp,
        "fingerprint_source": source,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write(path, data)
    return data


def load_device(path: Path | None = None, *, check: bool = True) -> dict[str, Any]:
    """Read ``device.json``; with ``check``, refuse a file from another machine."""
    path = path or device_path()
    if not path.exists():
        raise DeviceError(
            "missing", "no device identity: run mool device init "
            "(opt-in; creates ~/.moolmesh/device.json)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        bytes.fromhex(data["secret"])
        if len(data["secret"]) < 32 or not data.get("fingerprint"):
            raise ValueError("incomplete")
    except (OSError, ValueError, KeyError, TypeError):
        raise DeviceError(
            "invalid", f"{path} is not a valid device file: "
            "run mool device init --reset") from None
    if check and not fingerprint_matches(data):
        raise DeviceError(
            "copied", "device.json does not belong to this machine (copied or "
            "synced from another one?): run mool device init --reset")
    return data


def fingerprint_matches(data: dict[str, Any]) -> bool:
    _, fp = machine_fingerprint()
    return hmac.compare_digest(str(data.get("fingerprint", "")), fp)
