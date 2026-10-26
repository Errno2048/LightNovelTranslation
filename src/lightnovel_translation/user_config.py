"""Reusable private configuration and non-secret diagnostics."""
from __future__ import annotations

from ._resources import skill_resources
import os
from pathlib import Path
import re
import subprocess
import json
import math
import tempfile
import tomllib

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


_SAFE_CONFIG_PATHS = {
    'capacity': {'context_window_tokens', 'max_output_tokens', 'max_input_tokens',
                 'token_inflation_factor', 'safety_tokens'},
    'index': {'parent_max_chapters', 'child_max_chapters', 'parent_min_chapters',
              'expected_l2_count', 'l2_prepartition_min_chapters', 'parent_build_mode',
              'entity_catalog_threshold', 'core_entity_limit', 'layout', 'child_target_tokens',
              'child_min_content_tokens', 'predecessor_reserve_tokens', 'protocol_reserve_tokens'},
    'limits': {'max_requests', 'max_input_tokens', 'max_output_tokens', 'max_attempts'},
    'references': {'packet_token_budget', 'style_max_documents', 'style_excerpt_paragraphs',
                   'terminology_max_terms', 'terminology_input_tokens', 'terminology_output_tokens',
                   'terminology_max_documents', 'terminology_radius', 'discover_terms'},
    'workflow': {'translation_concurrency', 'review_concurrency',
                 'translation_mode', 'pipeline_review_backlog',
                 'pipeline_manual_backlog', 'adjudication_mode', 'batch_size',
                 'full_chapters', 'history_chapters', 'summary_every', 'summary_lag',
                 'reference_chunk', 'max_revisions', 'translation_tokens',
                 'review_tokens', 'adjudication_tokens', 'summary_tokens',
                 'glossary_tokens', 'glossary_terms_per_chapter',
                 'glossary_terms_base', 'glossary_max_continuations',
                 'lookup_chapters', 'lookup_terms',
                 'max_term_checks', 'max_tool_rounds'},
    'syosetu': {'concurrency', 'request_interval'},
    'model': {'model', 'base_url', 'timeout', 'retries', 'thinking', 'reasoning_effort', 'trust_env'},
    'sequence': {'strategy', 'mode', 'n_chapter', 'n_child', 'compression_trigger',
                 'compression_target', 'max_revisions', 'translation_tools', 'targeted_evidence',
                 'targeted_evidence_tokens', 'output_profile', 'reference_profile', 'mapping_profile',
                 'parent_build_mode', 'entity_catalog_threshold', 'core_entity_limit',
                 'parent_min_chapters', 'parent_max_chapters', 'expected_l2_count',
                 'l2_prepartition_min_chapters', 'child_max_chapters', 'planning_utilization',
                 'child_min_content_tokens', 'parent_summary_input_tokens_per_child',
                 'soft_input_tokens', 'max_requests', 'max_input_tokens', 'max_output_tokens',
                 'max_attempts', 'max_attempts'},
}
_ROLE_NAMES = {'translator', 'reviewer', 'adjudicator', 'indexer', 'compressor',
               'mapping', 'revision', 'repair'}


def _toml_literal(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ConfigurationError('config update values must be strings, finite numbers or booleans')


def _allowed_update(path):
    parts = path.split('.')
    if len(parts) == 2 and parts[0] in _SAFE_CONFIG_PATHS and parts[1] in _SAFE_CONFIG_PATHS[parts[0]]:
        return parts
    if len(parts) == 3 and parts[0] == 'roles' and parts[1] in _ROLE_NAMES and parts[2] == 'requested_output_tokens':
        return parts
    raise ConfigurationError('config update path is not an allowed non-credential setting')


def _patch_toml(text, updates):
    lines = text.splitlines()
    for path, value in updates.items():
        parts = _allowed_update(path)
        section = '.'.join(parts[:-1])
        key = parts[-1]
        heading = f'[{section}]'
        section_start = next((i for i, line in enumerate(lines)
                              if line.strip() == heading), None)
        if section_start is None:
            if lines and lines[-1].strip():
                lines.append('')
            lines.extend([heading, f'{key} = {_toml_literal(value)}'])
            continue
        section_end = next((i for i in range(section_start + 1, len(lines))
                            if re.fullmatch(r'\s*\[[^]]+\]\s*(?:#.*)?', lines[i])), len(lines))
        assignment = re.compile(rf'^\s*{re.escape(key)}\s*=')
        key_line = next((i for i in range(section_start + 1, section_end)
                         if assignment.match(lines[i])), None)
        replacement = f'{key} = {_toml_literal(value)}'
        if key_line is None:
            lines.insert(section_end, replacement)
        else:
            lines[key_line] = replacement
    return '\n'.join(lines).rstrip() + '\n'


def update_user_config(updates: dict[str, object], directory: str | Path | None = None) -> dict:
    """Safely update allowlisted TOML settings without returning credential data."""
    if not isinstance(updates, dict) or not updates:
        raise ConfigurationError('updates must be a nonempty path/value mapping')
    if any(not isinstance(path, str) for path in updates):
        raise ConfigurationError('config update paths must be strings')
    for path in updates:
        _allowed_update(path)
    path = user_config_path(directory)
    if not path.is_file():
        raise ConfigurationError('user configuration does not exist; initialize it first')
    original = path.read_text(encoding='utf-8-sig')
    updated = _patch_toml(original, updates)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='\n',
                dir=path.parent, prefix='.config-update-', suffix='.toml', delete=False) as stream:
            temp_name = stream.name
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary = Path(temp_name)
        _private_permissions(temporary)
        from .config import TranslationConfig
        TranslationConfig.from_dict(tomllib.loads(updated), base=path.parent)
        os.replace(temporary, path)
        _private_permissions(path)
    except (ConfigurationError, OSError, ValueError):
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        raise ConfigurationError('configuration update failed validation; existing file preserved') from None
    status = user_config_status(directory)
    return {'path': str(path), 'updated_fields': sorted(updates), 'valid': status['valid'],
            'generation_ready': status['generation_ready'], 'credential_present': status['credential_present']}
