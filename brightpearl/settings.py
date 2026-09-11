"""Global settings for Brightpearl tooling."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import re
from typing import Dict


LOG_LEVELS: Dict[str, int] = {
    "PAYLOAD": 5,
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


@dataclass(frozen=True)
class AppSettings:
    log_level: str = "INFO"
    log_output_dir: str = "output/debug"
    throttle_threshold: int = 2
    menu_font_size: int = 11
    submenu_font_size: int = 11
    response_font_size: int = 11
    json_font_size: int = 11
    start_resolution: str = "1200x600"
    appearance_mode: str = "System"
    appearance_theme: str = "Sage"
    unmatched_output_dir: str = "output/exceptions"
    download_max_retries: int = 3
    download_default_sleep_ms: int = 200
    upload_max_retries: int = 3
    upload_default_sleep_ms: int = 200
    stock_correction_batch_size: int = 50


_SETTINGS: AppSettings | None = None


def settings_path() -> str:
    os.makedirs("db", exist_ok=True)
    return os.path.join("db", "settings.json")


def _coerce_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _coerce_log_level(value: str, default: str) -> str:
    if not value:
        return default
    candidate = str(value).upper()
    return candidate if candidate in LOG_LEVELS else default


def _coerce_resolution(value: str, default: str) -> str:
    if not value:
        return default
    candidate = str(value).strip()
    if re.match(r"^\d+x\d+$", candidate):
        return candidate
    return default


def _coerce_appearance(value: str, default: str) -> str:
    if not value:
        return default
    candidate = str(value)
    return candidate if candidate in {"System", "Light", "Dark"} else default


def _coerce_appearance_theme(value: str, default: str) -> str:
    if not value:
        return default
    candidate = str(value)
    return candidate if candidate in {"Brightpearl", "Sage"} else default


def normalize_settings(raw: dict) -> AppSettings:
    """Normalize raw settings into a validated AppSettings object."""
    defaults = AppSettings()
    unmatched_output_dir = raw.get("unmatched_output_dir") or defaults.unmatched_output_dir
    unmatched_output_dir = os.path.expandvars(os.path.expanduser(unmatched_output_dir))
    log_output_dir = raw.get("log_output_dir") or defaults.log_output_dir
    log_output_dir = os.path.expandvars(os.path.expanduser(log_output_dir))
    return AppSettings(
        log_level=_coerce_log_level(raw.get("log_level"), defaults.log_level),
        log_output_dir=log_output_dir,
        throttle_threshold=_coerce_int(raw.get("throttle_threshold"), defaults.throttle_threshold),
        menu_font_size=_coerce_int(raw.get("menu_font_size"), defaults.menu_font_size),
        submenu_font_size=_coerce_int(raw.get("submenu_font_size"), defaults.submenu_font_size),
        response_font_size=_coerce_int(raw.get("response_font_size"), defaults.response_font_size),
        json_font_size=_coerce_int(raw.get("json_font_size"), defaults.json_font_size),
        start_resolution=_coerce_resolution(raw.get("start_resolution"), defaults.start_resolution),
        appearance_mode=_coerce_appearance(raw.get("appearance_mode"), defaults.appearance_mode),
        appearance_theme=_coerce_appearance_theme(raw.get("appearance_theme"), defaults.appearance_theme),
        unmatched_output_dir=unmatched_output_dir,
        download_max_retries=_coerce_int(raw.get("download_max_retries"), defaults.download_max_retries),
        download_default_sleep_ms=_coerce_int(raw.get("download_default_sleep_ms"), defaults.download_default_sleep_ms),
        upload_max_retries=_coerce_int(raw.get("upload_max_retries"), defaults.upload_max_retries),
        upload_default_sleep_ms=_coerce_int(raw.get("upload_default_sleep_ms"), defaults.upload_default_sleep_ms),
        stock_correction_batch_size=_coerce_bounded_int(
            raw.get("stock_correction_batch_size"),
            defaults.stock_correction_batch_size,
            1,
            500,
        ),
    )


def load_settings() -> AppSettings:
    path = settings_path()
    if not os.path.exists(path):
        return AppSettings()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    return normalize_settings(data or {})


def get_settings() -> AppSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def save_settings(settings: AppSettings) -> None:
    global _SETTINGS
    path = settings_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(asdict(settings), handle, indent=2, sort_keys=True)
    _SETTINGS = settings


def should_log(level: str) -> bool:
    current = get_settings().log_level
    return LOG_LEVELS.get(level.upper(), LOG_LEVELS["INFO"]) >= LOG_LEVELS.get(current, LOG_LEVELS["INFO"])


def get_download_retry_settings() -> tuple[int, int]:
    settings = get_settings()
    return settings.download_max_retries, settings.download_default_sleep_ms


def get_upload_retry_settings() -> tuple[int, int]:
    settings = get_settings()
    return settings.upload_max_retries, settings.upload_default_sleep_ms
