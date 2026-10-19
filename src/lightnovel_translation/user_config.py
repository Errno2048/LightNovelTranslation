"""Reusable private configuration and non-secret diagnostics."""
from __future__ import annotations

from ._resources import skill_resources
import os
from pathlib import Path
import re
import subprocess

from .errors import ConfigurationError


def user_config_directory(directory: str | Path | None = None) -> Path:
    value = directory if directory is not None else os.environ.get("LIGHTNOVEL_TRANSLATION_HOME")
    return (Path(value).expanduser() if value is not None else Path.home() / ".lightnovel_translation").resolve()


def user_config_path(directory: str | Path | None = None) -> Path:
    return user_config_directory(directory) / "config.toml"


def _private_permissions(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    # SID-based grants avoid localized account names and command interpolation.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        result = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                                capture_output=True, check=True, timeout=10)
        match = re.search(rb"S-1-\d+(?:-\d+)+", result.stdout)
        if match is None:
            raise ValueError("invalid SID")
        sid = match.group().decode("ascii")
        # Apply a protected DACL directly. Shell ACL utilities can hang when
        # launched under managed Windows environments; no helper process is
        # needed for the actual permission change.
        import ctypes
        from ctypes import wintypes
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        apply = advapi.SetFileSecurityW
        apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        apply.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        flags = "OICI" if directory else ""
        descriptor = ctypes.c_void_p()
        sddl = f"D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)"
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError("invalid security descriptor")
        try:
            if not apply(str(path), 0x80000004, descriptor):  # protected DACL
                raise OSError("could not set configuration permissions")
        finally:
            kernel.LocalFree(descriptor)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        raise ConfigurationError("could not restrict user configuration permissions") from None


def init_user_config(directory: str | Path | None = None) -> dict:
    """Create an empty template, exclusively. Never read or overwrite an existing key."""
    folder = user_config_directory(directory)
    created_directory = False
    try:
        folder.mkdir(parents=True, mode=0o700, exist_ok=False)
        created_directory = True
    except FileExistsError:
        if not folder.is_dir():
            raise ConfigurationError("user configuration directory is not a directory") from None
    if created_directory:
        _private_permissions(folder, directory=True)
    path = user_config_path(folder)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"path": str(path), "created": False, "status": "already_exists"}
    template = skill_resources().joinpath("assets/config.example.toml").read_text(encoding="utf-8")
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(template)
        stream.flush()
        os.fsync(stream.fileno())
    _private_permissions(path)
    return {"path": str(path), "created": True, "status": "needs_local_edit"}


def user_config_status(directory: str | Path | None = None) -> dict:
    """Parse locally and expose readiness booleans/field names, never key contents."""
    from .config import TranslationConfig
    path = user_config_path(directory)
    result = {"path": str(path), "exists": path.is_file(), "valid": False,
              "credential_present": False, "generation_ready": False}
    if not result["exists"]:
        return {**result, "error": "config_not_found"}
    try:
        config = TranslationConfig.from_file(path)
    except (ConfigurationError, ValueError, OSError):
        return {**result, "error": "invalid_configuration"}
    missing = config.generation_issues()
    return {**result, "valid": True, "credential_present": bool(config.model and
            config.model.api_key.strip() and "model.api_key" not in missing),
            "generation_ready": not missing, "missing_fields": missing}
